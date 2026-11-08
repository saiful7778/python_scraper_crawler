"""Searches each keyword on Google Maps, with proxy rotation and recovery."""

import logging
import threading
import time
from collections.abc import Callable, Sequence

from app.services.browser import BrowserSession, CDPError
from app.services.google_map_scraper.extract_details import PlaceRecord
from app.utils.errors import describe
from app.services.google_map_scraper.google_map import (
    build_maps_search_url,
    collect_leads,
)
from app.services.google_map_scraper.google_map.parsing import BatchStats, PlaceLink
from app.services.google_map_scraper.job.lead_rows import (
    build_lead_row,
    lead_identity_key,
)
from app.utils.failure_messages import is_proxy_failure, is_timeout_failure
from app.utils.proxies import ProxyRotation

from .job_failures import LOGIN_REFUSED
from .job_limits import (
    BACKOFF_SECONDS,
    MAX_BROWSER_RELAUNCHES,
    NO_FEED_RETRY_MARGIN_SECONDS,
    ROTATE_WAIT_SECONDS,
)
from .job_outcome import search_percent
from .job_progress import JobProgress
from .job_settings import JobSettings
from .job_status import SearchEndReason
from .job_store import JobStore
from .job_types import LeadRow
from .run_state import RunState

logger = logging.getLogger(__name__)


class KeywordSearch:
    """Runs the Maps search for every keyword of a job, from ``first_keyword`` on."""

    def __init__(
        self,
        *,
        job_id: str,
        settings: JobSettings,
        store: JobStore,
        browser: BrowserSession,
        rotation: ProxyRotation,
        progress: JobProgress,
        state: RunState,
        stop: threading.Event,
        deadline: float,
        log: Callable[[str], object],
    ):
        """Wire the search to the job's store, browser, progress and shared state."""
        self._job_id = job_id
        self._settings = settings
        self._store = store
        self._browser = browser
        self._rotation = rotation
        self._progress = progress
        self._state = state
        self._stop = stop
        self._deadline = deadline
        self._log = log
        self._index = 0
        self._keyword = ""

    def run(self, first_keyword: int) -> None:
        """Search keywords in order until all are done, the time runs out or the job stops."""
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
                    self._log(
                        f"job {self._job_id} keyword {self._position()}: browser stopped ({message}); starting it again"
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
                    self._log(
                        f"job {self._job_id} keyword {self._position()}: the proxy failed; retry "
                        f"{self._state.retries}/{len(BACKOFF_SECONDS)} through the next proxy"
                    )
                    continue
                self._state.errors.append(describe(error))
                logger.error(
                    "job %s keyword %s failed: %s",
                    self._job_id,
                    self._position(),
                    describe(error),
                    exc_info=True,
                )
                self._log(
                    f"job {self._job_id} keyword {self._position()}: error: {describe(error)}"
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
                size = self._store.append_rows(self._job_id, [])
                if size is None:
                    break
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
                logger.error(
                    "keyword %s failed while reading its results: %s",
                    self._position(),
                    describe(error),
                    exc_info=True,
                )
                self._state.errors.append(
                    f"keyword {self._position()}: {describe(error)}"
                )
                size = self._store.append_rows(self._job_id, [])
                if size is None:
                    break
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
                        self._log(
                            f"job {self._job_id} keyword {self._position()}: Google's unusual-traffic page; "
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
                self._log(
                    f"job {self._job_id} keyword {self._position()}: Google's unusual-traffic page; stopping"
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
                self._log(
                    f"job {self._job_id} keyword {self._position()}: no result list; one more try"
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

            size = self._store.append_rows(self._job_id, [])
            if size is None:
                break
            if SearchEndReason.is_error(reason):
                self._state.errors.append(SearchEndReason.error_message(reason))
            self._progress.save(
                done=index + 1,
                saved=size,
                percent=int(100 * (index + 1) / len(keywords)),
                failed=len(self._state.errors),
                last_error=self._state.last_error,
            )
            self._log(
                f"job {self._job_id} keyword {self._position()}: {len(leads)} leads, "
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
                    log=lambda _message: None,
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
        """Store leads not yet seen, including those no streamed batch carried."""
        fresh: list[LeadRow] = []
        for lead in leads:
            if not self._has_room(len(fresh)):
                break
            key = lead_identity_key(lead)
            if key and key not in self._state.seen_keys:
                self._state.seen_keys.add(key)
                fresh.append(build_lead_row(lead, self._keyword))
        self._state.rows.extend(fresh)
        if fresh and self._store.append_rows(self._job_id, fresh) is None:
            self._stop.set()
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
