"""The background thread that claims pending scrapes and runs them one at a time."""

import threading
from collections.abc import Callable

from redis import Redis

from app.services.buffer.client import BufferError
from app.utils.errors import describe
from app.utils.logger import get_logger

from .scrape_database import ScrapeDatabase, ScrapeDatabaseError
from .scrape_run import RunOutcome, ScrapeRun
from .scrape_status import ScrapeStatus
from .scrape_types import LeadRow, ScrapeRecord

logger = get_logger(__name__)

_IDLE_POLL_SECONDS = 5
_RETRY_PAUSE_SECONDS = 2


def _reason(error: Exception) -> str:
    return f"{type(error).__name__}: {error}"


class ScrapeWorker(threading.Thread):
    """Claims the oldest pending scrape, runs it, and repeats until shut down.

    A Redis or PostgreSQL failure stops the worker and is kept in :attr:`failure`
    for the runner to raise; the scrape keeps its Redis data and resumes later.
    """

    def __init__(
        self,
        database: ScrapeDatabase,
        redis: Redis,
        headless: bool = True,
        known_keys: frozenset[str] = frozenset(),
        on_finished: Callable[[str, RunOutcome, list[LeadRow]], None] | None = None,
    ):
        """Create the worker thread; call :meth:`start` to begin.

        ``on_finished`` receives each scrape's outcome and accepted leads when
        its run ends, whether it finished or failed. A paused run reports nothing.
        """
        super().__init__(daemon=True, name="scrapes")
        self._database = database
        self._redis = redis
        self._headless = headless
        self._known_keys = known_keys
        self._on_finished = on_finished
        self._wake = threading.Event()
        self._stopped = threading.Event()
        self._current: ScrapeRun | None = None
        self.failure: BufferError | ScrapeDatabaseError | None = None

    def run(self) -> None:
        """Thread body: process scrapes until :meth:`shutdown` is called."""
        while not self._stopped.is_set():
            stage = "claiming the next pending scrape"
            try:
                scrape = self._database.claim_next_pending()
                if scrape is None:
                    self._wake.wait(_IDLE_POLL_SECONDS)
                    self._wake.clear()
                    continue
                stage = f"running scrape {scrape['ID']}"
                self._execute(scrape)
            except (BufferError, ScrapeDatabaseError) as error:
                logger.error("the scrape worker stopped: %s", describe(error))
                self.failure = error
                self._stopped.set()
            except Exception:
                logger.exception("worker failed while %s", stage)
                self._stopped.wait(_RETRY_PAUSE_SECONDS)

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
        run = ScrapeRun(
            self._database, scrape, self._headless, self._redis, self._known_keys
        )
        self._current = run
        logger.info("scrape %s: starting", scrape["ID"])
        if self._stopped.is_set():
            run.pause()
        try:
            run.execute()
        except (BufferError, ScrapeDatabaseError):
            raise
        except Exception as error:
            logger.exception("scrape %s failed", scrape["ID"])
            self._report(scrape, RunOutcome(ScrapeStatus.FAILED, _reason(error)), run)
            return
        finally:
            self._current = None
        if run.outcome is not None:
            self._report(scrape, run.outcome, run)

    def _report(self, scrape: ScrapeRecord, outcome: RunOutcome, run: ScrapeRun) -> None:
        logger.info(
            "scrape %s reported as %s with %d leads",
            scrape["ID"],
            outcome.status,
            len(run.rows),
        )
        if self._on_finished is not None:
            self._on_finished(scrape["ID"], outcome, run.rows)
