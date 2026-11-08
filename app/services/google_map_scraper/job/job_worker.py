"""The background thread that claims pending jobs and runs them one at a time."""

import logging
import threading
from collections.abc import Callable

from .job_run import JobRun
from .job_status import JobStatus
from .job_store import JobStore
from .job_types import JobRecord

logger = logging.getLogger(__name__)

_IDLE_POLL_SECONDS = 5
_RETRY_PAUSE_SECONDS = 2
_STATUS_WRITE_ATTEMPTS = 2


class JobWorker(threading.Thread):
    """Claims the oldest pending job, runs it, and repeats until shut down."""

    def __init__(
        self,
        store: JobStore,
        log: Callable[[str], object] = print,
        headless: bool = True,
    ):
        """Create the worker thread; call :meth:`start` to begin."""
        super().__init__(daemon=True, name="jobs")
        self._store = store
        self._log = log
        self._headless = headless
        self._wake = threading.Event()
        self._stopped = threading.Event()
        self._current: JobRun | None = None

    def run(self) -> None:
        """Thread body: process jobs until :meth:`shutdown` is called."""
        while not self._stopped.is_set():
            job: JobRecord | None = None
            stage = "claiming the next pending job"
            try:
                job = self._store.claim_next_pending()
                if job is None:
                    self._wake.wait(_IDLE_POLL_SECONDS)
                    self._wake.clear()
                    continue
                stage = f"running job {job['ID']}"
                self._execute(job)
            except Exception as error:
                logger.error("worker failed while %s", stage, exc_info=True)
                self._record_failure(job, error)

    def cancel(self, job_id: str) -> None:
        """Stop the running job if it is ``job_id`` (used when it is deleted)."""
        run = self._current
        if run is not None and run.job_id == job_id:
            run.cancel()

    def shutdown(self, wait: float = 0) -> None:
        """Pause the running job and stop the worker; optionally wait for it."""
        self._stopped.set()
        self._wake.set()
        run = self._current
        if run is not None:
            run.pause()
        if wait and self.is_alive():
            self.join(wait)

    def _execute(self, job: JobRecord) -> None:
        run = JobRun(self._store, job, self._log, self._headless)
        self._current = run
        if self._stopped.is_set():
            run.pause()
        try:
            run.execute()
        finally:
            self._current = None

    def _record_failure(self, job: JobRecord | None, error: Exception) -> None:
        reason = f"{type(error).__name__}: {error}"
        if job:
            for attempt in range(1, _STATUS_WRITE_ATTEMPTS + 1):
                try:
                    self._store.update_status(job["ID"], JobStatus.FAILED, reason)
                    break
                except Exception:
                    logger.error(
                        "could not mark job %s as failed (attempt %d of %d)",
                        job["ID"],
                        attempt,
                        _STATUS_WRITE_ATTEMPTS,
                        exc_info=True,
                    )
                    self._stopped.wait(_RETRY_PAUSE_SECONDS)
        try:
            self._log(f"job {job['ID'] if job else '-'} failed: {reason}")
        except Exception:
            logger.error("could not write the failure log line", exc_info=True)
        self._stopped.wait(_RETRY_PAUSE_SECONDS)
