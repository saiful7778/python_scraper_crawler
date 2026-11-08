"""Runs one scraping job from its saved progress to a final status."""

import logging
import threading
import time
from collections.abc import Callable
from typing import cast

from app.services.browser import BrowserError, BrowserSession, CDPError
from app.utils.errors import describe
from app.utils.proxies import ProxyRotation

from .job_limits import WATCHDOG_GRACE_SECONDS, WEBSITE_MIN_SECONDS
from .job_outcome import summarize_outcome
from .job_progress import JobProgress
from .job_settings import JobSettings
from .job_status import JobStage, JobStatus
from .job_store import JobStore
from .job_types import JobRecord, ProgressData
from .keyword_search import KeywordSearch
from .lead_rows import lead_identity_key
from .review_pass import ReviewPass
from .run_state import RunState
from .website_enrichment import ContactOptions, WebsiteEnricher

logger = logging.getLogger(__name__)


class JobRun:
    """Executes one job, resuming it from the stage and keyword it was saved at.

    The run ends in one of three ways: it finishes and stores its status, it is
    paused because the engine is shutting down (it resumes on the next start),
    or it is cancelled because the job was deleted.
    """

    def __init__(
        self,
        store: JobStore,
        job: JobRecord,
        log: Callable[[str], object],
        headless: bool,
    ):
        """Read the job's settings and progress; nothing runs until :meth:`execute`."""
        self.job_id: str = job["ID"]
        self.settings = JobSettings.model_validate(job["Data"])
        self._store = store
        self._saved_progress: ProgressData = job["Progress"].copy()
        self._log = log
        self._stop = threading.Event()
        self._pausing = False
        self._browser = BrowserSession(self.settings.locale_tag, headless)
        self._rotation = ProxyRotation(self.settings.proxies)
        self._state = RunState()
        self._progress: JobProgress | None = None
        self._deadline = 0.0

    def cancel(self) -> None:
        """Stop the run because its job was deleted."""
        self._stop.set()

    def pause(self) -> None:
        """Stop the run so it can resume on the next engine start."""
        self._pausing = True
        self._stop.set()
        self._browser.kill()

    def execute(self) -> None:
        """Run the job's remaining stages, then store its result or pause it."""
        if self._store.get_job(self.job_id) is None:
            return
        saved = self._saved_progress
        keywords = self.settings.keywords
        seconds_used = saved.get("elapsed", 0)
        started = time.monotonic()
        budget = max(0.0, self.settings.max_seconds - seconds_used)
        self._deadline = started + budget
        stage = saved.get("stage", JobStage.SEARCH)
        first_keyword = min(saved.get("done", 0), len(keywords))
        reviews = stage in (JobStage.SEARCH, JobStage.REVIEWS)

        if stage == JobStage.SEARCH:
            self._store.truncate_rows(self.job_id, saved.get("saved", 0))
        rows = self._store.read_rows(self.job_id)
        last_error = saved.get("last_error", "")
        self._state = RunState(
            rows=rows,
            seen_keys={k for k in map(lead_identity_key, rows) if k},
            errors=[last_error] * saved.get("failed", 0) if last_error else [],
            blocked=saved.get("blocked", False),
            retries=saved.get("retried", 0),
            timed_out=saved.get("timed_out", False),
        )
        self._progress = JobProgress(
            self._store,
            self.job_id,
            saved,
            rows,
            started,
            seconds_used,
        )
        self._progress.save(stage=stage, done=first_keyword)
        if stage == JobStage.SEARCH and first_keyword < len(keywords) and budget <= 0:
            self._state.timed_out = True

        wants_browser = (
            stage == JobStage.SEARCH and first_keyword < len(keywords)
        ) or reviews
        watchdog = self._start_watchdog(budget)
        try:
            if wants_browser and budget > 0 and not self._stop.is_set():
                self._browser.start()
            if stage == JobStage.SEARCH and self._browser.browser is not None:
                self._search(first_keyword)
            if stage == JobStage.SEARCH and not self._stop.is_set():
                stage = JobStage.REVIEWS if reviews else JobStage.WEBSITES
                self._save_search_end(stage)
            if (
                stage == JobStage.REVIEWS
                and rows
                and self._browser.browser is not None
                and not (self._stop.is_set() or self._state.blocked)
            ):
                self._run_reviews()
            if stage == JobStage.REVIEWS and not self._stop.is_set():
                stage = JobStage.WEBSITES
                self._progress.save(stage=stage)
        except (BrowserError, CDPError, OSError) as error:
            logger.error(
                "job %s stopped in stage %s: %s",
                self.job_id,
                stage,
                describe(error),
                exc_info=True,
            )
            self._state.errors.append(describe(error))
            self._log(f"job {self.job_id}: {describe(error)}")
        finally:
            watchdog.cancel()
            self._browser.close()

        if (
            stage == JobStage.WEBSITES
            and (self.settings.collect_emails or self.settings.collect_socials)
            and rows
            and not self._stop.is_set()
        ):
            self._run_websites()

        if self._pausing and self._store.get_job(self.job_id) is not None:
            self._pause(stage)
            return
        self._finish()

    def _search(self, first_keyword: int) -> None:
        assert self._progress is not None
        KeywordSearch(
            job_id=self.job_id,
            settings=self.settings,
            store=self._store,
            browser=self._browser,
            rotation=self._rotation,
            progress=self._progress,
            state=self._state,
            stop=self._stop,
            deadline=self._deadline,
            log=self._log,
        ).run(first_keyword)

    def _save_search_end(self, stage: str) -> None:
        assert self._progress is not None
        state = self._state
        self._progress.save(
            stage=stage,
            saved=self._store.append_rows(self.job_id, []) or 0,
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
        self._store.replace_rows(self.job_id, self._state.rows)
        with_place_id = sum(1 for r in self._state.rows if r.get("place_id"))
        self._log(f"job {self.job_id}: reviews for {reviewed}/{with_place_id} leads")

    def _run_websites(self) -> None:
        assert self._progress is not None
        options = ContactOptions(
            emails=self.settings.collect_emails,
            socials=self.settings.collect_socials,
            use_browser=self.settings.use_browser_for_websites,
        )
        result = WebsiteEnricher().enrich(
            self._state.rows,
            max(self._deadline - time.monotonic(), cast(float, WEBSITE_MIN_SECONDS)),
            options,
            self._stop.is_set,
        )
        with_website = sum(1 for r in self._state.rows if r["website"])
        self._log(
            f"job {self.job_id}: website data for {result.rows_enriched}/{with_website} websites"
        )
        if result.unchecked and not self._stop.is_set():
            self._state.notes.append(
                f"{result.unchecked} website(s) not checked: ran out of time"
            )

    def _pause(self, stage: str) -> None:
        assert self._progress is not None
        if stage != JobStage.SEARCH:
            self._store.replace_rows(self.job_id, self._state.rows)
        self._progress.save()
        self._store.update_status(self.job_id, JobStatus.PENDING)
        done = self._progress.data.get("done", 0)
        self._log(
            f"job {self.job_id} paused at keyword {done + 1}/{len(self.settings.keywords)} "
            f"({stage}): resumes when the engine starts again"
        )

    def _finish(self) -> None:
        assert self._progress is not None
        state = self._state
        self._progress.stamp(stage=JobStage.DONE)
        status, note = summarize_outcome(
            has_rows=bool(state.rows),
            errors=state.errors,
            blocked=state.blocked,
            timed_out=state.timed_out,
            notes=state.notes,
            retries=state.retries,
        )
        if not self._store.finish_job(
            self.job_id, state.rows, status, note, self._progress.data
        ):
            self._log(f"job {self.job_id} deleted while it ran")
            return
        self._log(
            f"job {self.job_id} {status}: {len(state.rows)} leads"
            + (f": {note}" if status == JobStatus.FAILED else "")
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
            logger.error("watchdog could not kill the browser", exc_info=True)
            self._log(f"job {self.job_id}: watchdog could not kill the browser")
            return
        self._log(
            f"job {self.job_id}: watchdog stopped browser {WATCHDOG_GRACE_SECONDS} s after max_time"
        )
