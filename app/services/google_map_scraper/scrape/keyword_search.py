"""Searches each keyword on Google Maps, with proxy rotation and recovery."""

import threading
import time
from collections.abc import Callable, Sequence

from redis.exceptions import RedisError

from app.services.browser import BrowserSession, CDPError
from app.services.buffer.client import BufferError
from app.services.google_map_scraper.extract_details import PlaceRecord
from app.services.google_map_scraper.google_map import (
    build_maps_search_url,
    collect_leads,
)
from app.services.google_map_scraper.google_map.parsing import BatchStats, PlaceLink
from app.services.google_map_scraper.scrape.lead_rows import build_lead_row
from app.utils.errors import describe
from app.utils.failure_messages import is_proxy_failure, is_timeout_failure
from app.utils.logger import get_logger
from app.utils.proxies import ProxyRotation

from .run_state import RunState
from .scrape_failures import LOGIN_REFUSED
from .scrape_limits import (
    BACKOFF_SECONDS,
    MAX_BROWSER_RELAUNCHES,
    NO_FEED_RETRY_MARGIN_SECONDS,
    ROTATE_WAIT_SECONDS,
)
from .scrape_outcome import search_percent
from .scrape_progress import ScrapeProgress
from .scrape_settings import ScrapeSettings
from .scrape_status import SearchEndReason
from .scrape_types import LeadRow

logger = get_logger(__name__)


class KeywordSearch:
    """Runs the Maps search for every keyword of a scrape, from ``first_keyword`` on."""

    def __init__(
        self,
        *,
        scrape_id: str,
        settings: ScrapeSettings,
        accept: Callable[[list[LeadRow]], list[LeadRow]],
        browser: BrowserSession,
        rotation: ProxyRotation,
        progress: ScrapeProgress,
        state: RunState,
        stop: threading.Event,
        deadline: float,
    ):
        """Wire the search to the scrape's accept step, browser, progress and shared state."""
        self._scrape_id = scrape_id
        self._settings = settings
        self._accept = accept
        self._browser = browser
        self._rotation = rotation
        self._progress = progress
        self._state = state
        self._stop = stop
        self._deadline = deadline
        self._index = 0
        self._keyword = ""

    def run(self, first_keyword: int) -> None:
        """Search keywords in order until all are done, the time runs out or the scrape stops."""
        keywords = self._settings.keywords
        index, again, relaunches = first_keyword, False, 0
        while (
            index < len(keywords)
            and self._browser.browser is not None
            and self._has_room()
        ):
            time_left = self._time_left()
            if time_left <= 0:
                self._state.timed_out = True
            if self._stop.is_set() or time_left <= 0:
                break
            self._index, self._keyword = index, keywords[index]
            proxy = self._rotation.pick(index)
            url = build_maps_search_url(
                self._keyword,
                self._settings.latitude,
                self._settings.longitude,
                self._settings.zoom,
                self._settings.language,
                self._settings.country,
            )
            started = time.monotonic()
            try:
                leads, reason, stats = self._collect(proxy, url, time_left)
            except BufferError:
                raise
            except (CDPError, OSError) as error:
                message = str(error)
                if message == LOGIN_REFUSED:
                    self._state.refused_proxies.add(proxy)
                if is_proxy_failure(message):
                    self._rotation.mark_failed(proxy)
                if (
                    self._browser.is_dead
                    and not self._stop.is_set()
                    and relaunches < MAX_BROWSER_RELAUNCHES
                    and self._time_left() > 0
                ):
                    relaunches += 1
                    logger.info(
                        f"scrape {self._scrape_id} keyword {self._position()}: browser stopped ({message}); starting it again"
                    )
                    self._browser.restart()
                    continue
                if (
                    (is_proxy_failure(message) or is_timeout_failure(message))
                    and len(self._rotation) > 1
                    and self._state.retries < len(BACKOFF_SECONDS)
                    and len(self._state.refused_proxies) < self._rotation.distinct_count
                    and not self._browser.is_dead
                    and not self._stop.is_set()
                ):
                    self._state.retries += 1
                    self._rotation.advance()
                    logger.info(
                        f"scrape {self._scrape_id} keyword {self._position()}: the proxy failed; retry "
                        f"{self._state.retries}/{len(BACKOFF_SECONDS)} through the next proxy"
                    )
                    continue
                self._state.errors.append(describe(error))
                logger.exception(
                    "scrape %s keyword %s failed",
                    self._scrape_id,
                    self._position(),
                )
                logger.warning(
                    "scrape %s keyword %s: error: %s",
                    self._scrape_id,
                    self._position(),
                    describe(error),
                )
                all_proxies_refused = (
                    self._state.refused_proxies
                    and len(self._state.refused_proxies)
                    >= self._rotation.distinct_count
                )
                if self._browser.is_dead or all_proxies_refused:
                    self._note_skipped_keywords()
                    break
                if self._stop.is_set():
                    break
                size = len(self._state.rows)
                index += 1
                self._state.retries, again = 0, False
                self._progress.save(
                    done=index,
                    saved=size,
                    percent=int(100 * index / len(keywords)),
                    failed=len(self._state.errors),
                    last_error=self._state.last_error,
                )
                continue
            except Exception as error:
                logger.exception(
                    "keyword %s failed while reading its results",
                    self._position(),
                )
                self._state.errors.append(
                    f"keyword {self._position()}: {describe(error)}"
                )
                size = len(self._state.rows)
                index += 1
                self._state.retries, again = 0, False
                self._progress.save(
                    done=index,
                    saved=size,
                    percent=int(100 * index / len(keywords)),
                    failed=len(self._state.errors),
                    last_error=self._state.last_error,
                )
                continue

            self._record_batch(leads, stats["batches"])
            if self._stop.is_set():
                break

            if SearchEndReason.is_blocked(reason):
                if (
                    self._state.retries < len(BACKOFF_SECONDS)
                    and not self._stop.is_set()
                ):
                    wait = (
                        ROTATE_WAIT_SECONDS
                        if len(self._rotation) > 1
                        else BACKOFF_SECONDS[self._state.retries]
                    )
                    if time.monotonic() + wait < self._deadline:
                        self._state.retries += 1
                        self._rotation.advance()
                        logger.info(
                            f"scrape {self._scrape_id} keyword {self._position()}: Google's unusual-traffic page; "
                            f"retry {self._state.retries}/{len(BACKOFF_SECONDS)} in {wait} s"
                            + (
                                " through the next proxy"
                                if len(self._rotation) > 1
                                else ""
                            )
                        )
                        self._progress.save()
                        if self._stop.wait(wait):
                            break
                        continue
                self._state.blocked = True
                logger.warning(
                    "scrape %s keyword %s: Google's unusual-traffic page; stopping",
                    self._scrape_id,
                    self._position(),
                )
                break

            if (
                SearchEndReason.is_no_feed(reason)
                and not again
                and time.monotonic() + NO_FEED_RETRY_MARGIN_SECONDS < self._deadline
            ):
                again = True
                if len(self._rotation) > 1:
                    self._rotation.advance()
                logger.info(
                    f"scrape {self._scrape_id} keyword {self._position()}: no result list; one more try"
                )
                continue

            if SearchEndReason.is_no_feed(reason):
                self._state.notes.append(
                    f"keyword {index + 1}: Google showed no result list"
                    f"{' twice' if again else ''} "
                    "(no results for it, or a page the engine cannot read)"
                )
            if SearchEndReason.used_dom_fallback(reason):
                self._state.notes.append(
                    f"keyword {index + 1}: Google's result data could not be read, so "
                    + (
                        f"only names, links and coordinates were kept for {len(leads)} leads"
                        if leads
                        else "no leads were read"
                    )
                    + " (the engine may need an update)"
                )
            self._state.retries, again = 0, False
            if SearchEndReason.is_timeout(reason):
                self._state.timed_out = True

            size = len(self._state.rows)
            if SearchEndReason.is_error(reason):
                self._state.errors.append(SearchEndReason.error_message(reason))
            self._progress.save(
                done=index + 1,
                saved=size,
                percent=int(100 * (index + 1) / len(keywords)),
                failed=len(self._state.errors),
                last_error=self._state.last_error,
            )
            logger.info(
                f"scrape {self._scrape_id} keyword {self._position()}: {len(leads)} leads, "
                f"{len(self._state.rows)} in all, {stats['batches']} batches, "
                f"{time.monotonic() - started:.1f} s ({reason})"
            )
            if SearchEndReason.is_error(reason) and self._browser.is_dead:
                if (
                    self._stop.is_set()
                    or relaunches >= MAX_BROWSER_RELAUNCHES
                    or self._deadline <= time.monotonic()
                ):
                    self._note_skipped_keywords()
                    break
                relaunches += 1
                self._browser.restart()
            index += 1

    def _collect(
        self, proxy: str | None, url: str, seconds_left: float
    ) -> tuple[list[PlaceLink | PlaceRecord], str, BatchStats]:
        with self._browser.open_tab(proxy) as proxy_tab:
            try:
                if proxy_tab.tab is None:
                    raise CDPError("Browser tab is unavailable")
                leads, reason, stats = collect_leads(
                    proxy_tab.tab,
                    url,
                    depth=self._settings.scroll_depth,
                    max_seconds=seconds_left,
                    stop=lambda: self._stop.is_set() or not self._has_room(),
                    on_batch=self._record_batch,
                )
            except CDPError:
                if proxy_tab.refused:
                    raise CDPError(LOGIN_REFUSED) from None
                raise
            if proxy_tab.refused and not leads:
                raise CDPError(LOGIN_REFUSED)
            return leads, reason, stats

    def _record_batch(
        self, leads: Sequence[PlaceLink | PlaceRecord], batches: int
    ) -> None:
        """Accept leads not yet seen, including those no streamed batch carried."""
        try:
            for lead in leads:
                if not self._has_room():
                    break
                self._accept([build_lead_row(lead, self._keyword)])
        except RedisError as error:
            self._stop.set()
            raise BufferError(
                f"Redis failed while buffering leads: {describe(error)}"
            ) from error
        except BufferError:
            self._stop.set()
            raise
        self._progress.save(
            percent=search_percent(
                self._index,
                batches,
                self._settings.scroll_depth,
                len(self._settings.keywords),
            )
        )

    def _note_skipped_keywords(self) -> None:
        remaining = len(self._settings.keywords) - self._index - 1
        if not self._stop.is_set() and remaining > 0 and self._state.errors:
            self._state.errors[-1] += (
                f"; {remaining} keyword(s) after it were not searched"
            )

    def _has_room(self, pending: int = 0) -> bool:
        limit = self._settings.limit
        return limit is None or len(self._state.rows) + pending < limit

    def _position(self) -> str:
        return f"{self._index + 1}/{len(self._settings.keywords)}"

    def _time_left(self) -> float:
        return self._deadline - time.monotonic()
