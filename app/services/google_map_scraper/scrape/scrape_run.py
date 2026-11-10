"""Runs one scraping scrape from its saved progress to a final status."""

import threading
import time

from app.services.browser import BrowserError, BrowserSession, CDPError
from app.services.crawler.storage.crawl_store import CrawlStore
from app.utils.errors import describe
from app.utils.logger import get_logger
from app.utils.proxies import ProxyRotation

from .dedupe import seen_keys_of
from .keyword_search import KeywordSearch
from .review_pass import ReviewPass
from .run_state import RunState
from .scrape_limits import WATCHDOG_GRACE_SECONDS
from .scrape_outcome import summarize_outcome
from .scrape_progress import ScrapeProgress
from .scrape_settings import ScrapeSettings
from .scrape_status import ScrapeStage, ScrapeStatus
from .scrape_store import ScrapeStore
from .scrape_types import ProgressData, ScrapeRecord

logger = get_logger(__name__)


class ScrapeRun:
    """Executes one scrape, resuming it from the stage and keyword it was saved at.

    The run ends in one of three ways: it finishes and stores its status, it is
    paused because the engine is shutting down (it resumes on the next start),
    or it is cancelled because the scrape was deleted.
    """

    def __init__(
        self,
        store: ScrapeStore,
        scrape: ScrapeRecord,
        headless: bool,
        known_keys: frozenset[str] = frozenset(),
    ):
        """Read the scrape's settings and progress; nothing runs until :meth:`execute`."""
        self.scrape_id: str = scrape["ID"]
        self.settings = ScrapeSettings.model_validate(scrape["Data"])
        self._store = store
        self._crawl = CrawlStore(store)
        self._known_keys = known_keys
        self._saved_progress: ProgressData = scrape["Progress"].copy()
        self._stop = threading.Event()
        self._pausing = False
        self._browser = BrowserSession(self.settings.locale_tag, headless)
        self._rotation = ProxyRotation(self.settings.proxies)
        self._state = RunState()
        self._progress: ScrapeProgress | None = None
        self._deadline = 0.0

    def cancel(self) -> None:
        """Stop the run because its scrape was deleted."""
        self._stop.set()

    def pause(self) -> None:
        """Stop the run so it can resume on the next engine start."""
        self._pausing = True
        self._stop.set()
        self._browser.kill()

    def execute(self) -> None:
        """Run the scrape's remaining stages, then store its result or pause it."""
        if self._store.get_scrape(self.scrape_id) is None:
            return
        saved = self._saved_progress
        keywords = self.settings.keywords
        seconds_used = saved.get("elapsed", 0)
        started = time.monotonic()
        budget = max(0.0, self.settings.max_seconds - seconds_used)
        self._deadline = started + budget
        stage = saved.get("stage", ScrapeStage.SEARCH)
        first_keyword = min(saved.get("done", 0), len(keywords))
        reviews = stage in (ScrapeStage.SEARCH, ScrapeStage.REVIEWS)

        if stage == ScrapeStage.SEARCH:
            self._store.truncate_rows(self.scrape_id, saved.get("saved", 0))
        rows = self._store.read_rows(self.scrape_id)
        last_error = saved.get("last_error", "")
        self._state = RunState(
            rows=rows,
            seen_keys=seen_keys_of(rows) | self._known_keys,
            errors=[last_error] * saved.get("failed", 0) if last_error else [],
            blocked=saved.get("blocked", False),
            retries=saved.get("retried", 0),
            timed_out=saved.get("timed_out", False),
        )
        self._progress = ScrapeProgress(
            self._store,
            self.scrape_id,
            saved,
            rows,
            started,
            seconds_used,
        )
        self._progress.save(stage=stage, done=first_keyword)
        if (
            stage == ScrapeStage.SEARCH
            and first_keyword < len(keywords)
            and budget <= 0
        ):
            self._state.timed_out = True

        wants_browser = (
            stage == ScrapeStage.SEARCH and first_keyword < len(keywords)
        ) or reviews
        watchdog = self._start_watchdog(budget)
        try:
            if wants_browser and budget > 0 and not self._stop.is_set():
                self._browser.start()
            if stage == ScrapeStage.SEARCH and self._browser.browser is not None:
                self._search(first_keyword)
            if stage == ScrapeStage.SEARCH and not self._stop.is_set():
                stage = ScrapeStage.REVIEWS if reviews else ScrapeStage.WEBSITES
                self._save_search_end(stage)
            if (
                stage == ScrapeStage.REVIEWS
                and rows
                and self._browser.browser is not None
                and not (self._stop.is_set() or self._state.blocked)
            ):
                self._run_reviews()
            if stage == ScrapeStage.REVIEWS and not self._stop.is_set():
                stage = ScrapeStage.WEBSITES
                self._progress.save(stage=stage)
        except (BrowserError, CDPError, OSError) as error:
            logger.exception(
                "scrape %s stopped in stage %s",
                self.scrape_id,
                stage,
            )
            self._state.errors.append(describe(error))
            logger.warning("scrape %s: %s", self.scrape_id, describe(error))
        finally:
            watchdog.cancel()
            self._browser.close()

        if stage == ScrapeStage.WEBSITES and rows and not self._stop.is_set():
            self._queue_websites()

        if self._pausing and self._store.get_scrape(self.scrape_id) is not None:
            self._pause(stage)
            return
        self._finish()

    def _search(self, first_keyword: int) -> None:
        assert self._progress is not None
        KeywordSearch(
            scrape_id=self.scrape_id,
            settings=self.settings,
            store=self._store,
            browser=self._browser,
            rotation=self._rotation,
            progress=self._progress,
            state=self._state,
            stop=self._stop,
            deadline=self._deadline,
        ).run(first_keyword)

    def _save_search_end(self, stage: str) -> None:
        assert self._progress is not None
        state = self._state
        self._progress.save(
            stage=stage,
            saved=self._store.append_rows(self.scrape_id, []) or 0,
            blocked=state.blocked,
            retried=state.retries,
            failed=len(state.errors),
            last_error=state.last_error,
            timed_out=state.timed_out,
        )

    def _run_reviews(self) -> None:
        assert self._progress is not None
        reviewed = ReviewPass(
            browser=self._browser,
            settings=self.settings,
            deadline=self._deadline,
            stop=self._stop,
            proxies=self._rotation.healthy_proxies(),
            progress=self._progress,
            notes=self._state.notes,
        ).run(self._state.rows)
        self._store.replace_rows(self.scrape_id, self._state.rows)
        with_place_id = sum(1 for r in self._state.rows if r.get("place_id"))
        logger.info(
            "scrape %s: reviews for %s/%s leads",
            self.scrape_id,
            reviewed,
            with_place_id,
        )

    def _queue_websites(self) -> None:
        queued = self._crawl.queue_websites(self.scrape_id)
        logger.info(
            "scrape %s: queued %s websites for the crawler", self.scrape_id, queued
        )

    def _pause(self, stage: str) -> None:
        assert self._progress is not None
        if stage not in (ScrapeStage.SEARCH, ScrapeStage.WEBSITES):
            self._store.replace_rows(self.scrape_id, self._state.rows)
        self._progress.save()
        self._store.update_status(self.scrape_id, ScrapeStatus.PENDING)
        done = self._progress.data.get("done", 0)
        logger.info(
            "scrape %s paused at keyword %s/%s (%s): resumes when the engine starts again",
            self.scrape_id,
            done + 1,
            len(self.settings.keywords),
            stage,
        )

    def _finish(self) -> None:
        assert self._progress is not None
        state = self._state
        self._progress.stamp(stage=ScrapeStage.DONE)
        status, note = summarize_outcome(
            has_rows=bool(state.rows),
            errors=state.errors,
            blocked=state.blocked,
            timed_out=state.timed_out,
            notes=state.notes,
            retries=state.retries,
        )
        if not self._store.finish_scrape(
            self.scrape_id, state.rows, status, note, self._progress.data
        ):
            logger.info("scrape %s deleted while it ran", self.scrape_id)
            return
        logger.info(
            "scrape %s %s: %s leads%s",
            self.scrape_id,
            status,
            len(state.rows),
            f": {note}" if status == ScrapeStatus.FAILED else "",
        )

    def _start_watchdog(self, budget: float) -> threading.Timer:
        timer = threading.Timer(
            budget + float(WATCHDOG_GRACE_SECONDS), self._on_watchdog
        )
        timer.daemon = True
        timer.start()
        return timer

    def _on_watchdog(self) -> None:
        self._state.timed_out = True
        self._stop.set()
        try:
            self._browser.kill()
        except Exception:
            logger.exception("watchdog could not kill the browser")
            logger.warning(
                "scrape %s: watchdog could not kill the browser", self.scrape_id
            )
            return
        logger.info(
            "scrape %s: watchdog stopped browser %s s after max_time",
            self.scrape_id,
            WATCHDOG_GRACE_SECONDS,
        )
