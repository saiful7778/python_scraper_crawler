"""Runs one scraping scrape from its saved progress to a final status."""

import threading
import time
from dataclasses import dataclass

from redis import Redis

from app.services.browser import BrowserError, BrowserSession, CDPError
from app.services.buffer.known_keys import KnownKeys
from app.services.buffer.row_buffer import RowBuffer
from app.services.buffer.scrape_state import ScrapeState
from app.utils.errors import describe
from app.utils.logger import get_logger
from app.utils.proxies import ProxyRotation

from .keyword_search import KeywordSearch
from .lead_filter import keep_lead
from .review_pass import ReviewPass
from .run_state import RunState
from .scrape_database import ScrapeDatabase
from .scrape_limits import BUFFER_TTL_SECONDS, REVIEWS_SECONDS, WATCHDOG_GRACE_SECONDS
from .scrape_outcome import summarize_outcome
from .scrape_progress import ScrapeProgress
from .scrape_settings import ScrapeSettings
from .scrape_status import ScrapeStage, ScrapeStatus
from .scrape_types import LeadRow, ScrapeRecord
from .website_resolver import resolve_website

logger = get_logger(__name__)


@dataclass(frozen=True)
class RunOutcome:
    """How a run ended; the runner writes it to PostgreSQL once the crawl is done."""

    status: str
    note: str


def _latest_copies(rows: list[LeadRow]) -> list[LeadRow]:
    """One row per lead, in first-seen order, keeping the last copy.

    The review pass buffers a lead again with its reviews, so a resumed run
    reads both copies and keeps the reviewed one.
    """
    latest: dict[str, LeadRow] = {}
    for row in rows:
        latest[row.get("link") or row.get("title", "")] = row
    return list(latest.values())


class ScrapeRun:
    """Executes one scrape, resuming it from the stage and keyword it was saved at.

    The run ends in one of three ways: it finishes and stores its status, it is
    paused because the engine is shutting down (it resumes on the next start),
    or it is cancelled because the scrape was deleted.

    Leads, known keys, progress and websites live in Redis while the run is in
    progress; PostgreSQL keeps the scrape's record, and the runner finishes it
    from :attr:`outcome`.
    """

    def __init__(
        self,
        database: ScrapeDatabase,
        scrape: ScrapeRecord,
        headless: bool,
        redis: Redis,
        known_keys: frozenset[str] = frozenset(),
    ):
        """Read the scrape's settings; nothing runs until :meth:`execute`."""
        self.scrape_id: str = scrape["ID"]
        self.settings = ScrapeSettings.model_validate(scrape["Data"])
        self._database = database
        self._rows = RowBuffer(redis, BUFFER_TTL_SECONDS)
        self._known = KnownKeys(redis, BUFFER_TTL_SECONDS)
        self._scrape_state = ScrapeState(redis, BUFFER_TTL_SECONDS)
        self._known_keys = known_keys
        self.outcome: RunOutcome | None = None
        self._position_base = 0
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
        """Run the scrape's remaining stages, then set its outcome or pause it.

        A scrape whose state is still in Redis resumes from the progress kept
        there; otherwise it starts from the first keyword.
        """
        if self._database.get(self.scrape_id) is None:
            return
        resumed = self._scrape_state.get_settings(self.scrape_id) is not None
        if not resumed:
            self._scrape_state.create(
                self.scrape_id, self.settings.model_dump(by_alias=True, mode="json")
            )
        if self._known_keys or not resumed:
            self._known.load(self.scrape_id, self._known_keys)
        saved = self._scrape_state.get_progress(self.scrape_id)
        keywords = self.settings.keywords
        seconds_used = saved.get("elapsed", 0)
        started = time.monotonic()
        budget = max(0.0, self.settings.max_seconds - seconds_used)
        self._deadline = started + budget
        stage = saved.get("stage", ScrapeStage.SEARCH)
        first_keyword = min(saved.get("done", 0), len(keywords))
        reviews = stage in (ScrapeStage.SEARCH, ScrapeStage.REVIEWS)

        rows = _latest_copies(self._rows.read_all(self.scrape_id))
        if resumed:
            self._position_base = max(saved.get("leads", 0) - len(rows), 0)
        last_error = saved.get("last_error", "")
        self._state = RunState(
            rows=rows,
            errors=[last_error] * saved.get("failed", 0) if last_error else [],
            blocked=saved.get("blocked", False),
            retries=saved.get("retried", 0),
            timed_out=saved.get("timed_out", False),
        )
        self._progress = ScrapeProgress(
            self._scrape_state,
            self.scrape_id,
            saved,
            rows,
            started,
            seconds_used,
            self._position_base,
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
        watchdog = self._start_watchdog(budget + REVIEWS_SECONDS)
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

        if self._pausing and self._database.get(self.scrape_id) is not None:
            self._pause(stage)
            return
        self._finish()

    @property
    def rows(self) -> list[LeadRow]:
        """The leads accepted so far, in the order they were found."""
        return self._state.rows

    def accept(self, rows: list[LeadRow]) -> list[LeadRow]:
        """Buffer the leads not seen before and return them.

        A lead is seen before when its key is already known for this scrape, so
        leads are checked against the known keys in Redis, not against the rows.
        """
        accepted: list[LeadRow] = []
        try:
            for row in rows:
                if not keep_lead(row, self.settings):
                    continue
                if not self._known.add_if_new(self.scrape_id, row):
                    continue
                row["website"] = resolve_website(row["website"])
                accepted.append(row)
            if accepted:
                self._rows.append(self.scrape_id, accepted)
        except Exception:
            for row in accepted:
                self._known.forget(self.scrape_id, row)
            raise
        if not accepted:
            return accepted
        self._state.rows.extend(accepted)
        self._scrape_state.refresh_ttl(self.scrape_id)
        return accepted

    def _search(self, first_keyword: int) -> None:
        assert self._progress is not None
        KeywordSearch(
            scrape_id=self.scrape_id,
            settings=self.settings,
            accept=self.accept,
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
            saved=len(state.rows),
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
            deadline=time.monotonic() + REVIEWS_SECONDS,
            stop=self._stop,
            proxies=self._rotation.healthy_proxies(),
            progress=self._progress,
            notes=self._state.notes,
        ).run(self._state.rows)
        reviewed_rows = [r for r in self._state.rows if r.get("place_id")]
        self._rows.append(self.scrape_id, reviewed_rows)
        self._scrape_state.refresh_ttl(self.scrape_id)
        with_place_id = len(reviewed_rows)
        logger.info(
            "scrape %s: reviews for %s/%s leads",
            self.scrape_id,
            reviewed,
            with_place_id,
        )

    def _pause(self, stage: str) -> None:
        assert self._progress is not None
        self._progress.save()
        self._database.set_status(self.scrape_id, ScrapeStatus.PENDING)
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
            has_rows=self._progress.data["leads"] > 0,
            errors=state.errors,
            blocked=state.blocked,
            timed_out=state.timed_out,
            notes=state.notes,
            retries=state.retries,
        )
        self._progress.save()
        self.outcome = RunOutcome(status, note)
        logger.info(
            "scrape %s %s: %s leads%s",
            self.scrape_id,
            status,
            self._progress.data["leads"],
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
