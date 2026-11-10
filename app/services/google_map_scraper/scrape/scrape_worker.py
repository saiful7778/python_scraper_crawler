"""The background thread that claims pending scrapes and runs them one at a time."""

import threading

from app.utils.logger import get_logger

from .scrape_run import ScrapeRun
from .scrape_status import ScrapeStatus
from .scrape_store import ScrapeStore
from .scrape_types import ScrapeRecord

logger = get_logger(__name__)

_IDLE_POLL_SECONDS = 5
_RETRY_PAUSE_SECONDS = 2
_STATUS_WRITE_ATTEMPTS = 2


class ScrapeWorker(threading.Thread):
    """Claims the oldest pending scrape, runs it, and repeats until shut down."""

    def __init__(
        self,
        store: ScrapeStore,
        headless: bool = True,
        known_keys: frozenset[str] = frozenset(),
    ):
        """Create the worker thread; call :meth:`start` to begin."""
        super().__init__(daemon=True, name="scrapes")
        self._store = store
        self._headless = headless
        self._known_keys = known_keys
        self._wake = threading.Event()
        self._stopped = threading.Event()
        self._current: ScrapeRun | None = None

    def run(self) -> None:
        """Thread body: process scrapes until :meth:`shutdown` is called."""
        while not self._stopped.is_set():
            scrape: ScrapeRecord | None = None
            stage = "claiming the next pending scrape"
            try:
                scrape = self._store.claim_next_pending()
                if scrape is None:
                    self._wake.wait(_IDLE_POLL_SECONDS)
                    self._wake.clear()
                    continue
                stage = f"running scrape {scrape['ID']}"
                self._execute(scrape)
            except Exception as error:
                logger.exception("worker failed while %s", stage)
                self._record_failure(scrape, error)

    def cancel(self, scrape_id: str) -> None:
        """Stop the running scrape if it is ``scrape_id`` (used when it is deleted)."""
        run = self._current
        if run is not None and run.scrape_id == scrape_id:
            run.cancel()

    def shutdown(self, wait: float = 0) -> None:
        """Pause the running scrape and stop the worker; optionally wait for it."""
        self._stopped.set()
        self._wake.set()
        run = self._current
        if run is not None:
            run.pause()
        if wait and self.is_alive():
            self.join(wait)

    def _execute(self, scrape: ScrapeRecord) -> None:
        run = ScrapeRun(self._store, scrape, self._headless, self._known_keys)
        self._current = run
        if self._stopped.is_set():
            run.pause()
        try:
            run.execute()
        finally:
            self._current = None

    def _record_failure(self, scrape: ScrapeRecord | None, error: Exception) -> None:
        reason = f"{type(error).__name__}: {error}"
        if scrape:
            for attempt in range(1, _STATUS_WRITE_ATTEMPTS + 1):
                try:
                    self._store.update_status(scrape["ID"], ScrapeStatus.FAILED, reason)
                    break
                except Exception:
                    logger.exception(
                        "could not mark scrape %s as failed (attempt %d of %d)",
                        scrape["ID"],
                        attempt,
                        _STATUS_WRITE_ATTEMPTS,
                    )
                    self._stopped.wait(_RETRY_PAUSE_SECONDS)
        logger.error("scrape %s failed: %s", scrape["ID"] if scrape else "-", reason)
        self._stopped.wait(_RETRY_PAUSE_SECONDS)
