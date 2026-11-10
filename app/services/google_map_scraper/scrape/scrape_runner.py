import time
from dataclasses import dataclass
from typing import cast

from app.services.crawler.storage.crawl_store import CrawlStore
from app.services.crawler.worker import CrawlerWorker
from app.utils.errors import ScraperError
from app.utils.logger import get_logger

from .engine_config import EngineConfig
from .scrape_settings import ScrapeSettings
from .scrape_status import ScrapeStatus
from .scrape_store import ScrapeStore
from .scrape_types import LeadRow, SettingsData
from .scrape_worker import ScrapeWorker

logger = get_logger(__name__)

_POLL_SECONDS = 1


class ScrapeDeletedError(ScraperError):
    """The scrape was deleted while it was running, so it has no result."""


class ScrapeWorkerDiedError(ScraperError):
    """The scrape worker thread stopped before the scrape reached an outcome."""


@dataclass(frozen=True)
class ScrapeResult:
    """The outcome of one scrape run to its end."""

    scrape_id: str
    status: str
    note: str
    rows: list[LeadRow]
    blocked: bool


def _wait_for_websites(
    crawl: CrawlStore, crawler: CrawlerWorker, scrape_id: str, seconds: float
) -> None:
    deadline = time.monotonic() + seconds
    while (
        crawler.is_alive()
        and time.monotonic() < deadline
        and crawl.count_pending_websites(scrape_id)
    ):
        time.sleep(_POLL_SECONDS)


def run_scrape(
    name: str,
    settings: ScrapeSettings,
    *,
    config: EngineConfig | None = None,
    known_keys: frozenset[str] = frozenset(),
) -> ScrapeResult:
    """Store the scrape, run it through the engine and wait until it has an outcome.

    The scrape worker runs first. Then the crawler checks the scrape's websites
    for up to the scrape's ``max_seconds``, and the rows are read with the
    website data. Leads whose keys are in ``known_keys`` are skipped. The scrape
    and its crawl data are deleted from the store once its rows are read.

    :raises ScrapeDeletedError: If the scrape is deleted while it runs.
    :raises ScrapeWorkerDiedError: If the worker thread stops before the scrape
        has a status, so the wait would otherwise never end.
    """
    config = config or EngineConfig()
    store = ScrapeStore(str(config.data_dir))
    worker = ScrapeWorker(store, headless=config.headless, known_keys=known_keys)
    crawl = CrawlStore(store)
    crawler = CrawlerWorker(crawl)
    try:
        scrape_id = store.create_scrape(
            name,
            cast(SettingsData, settings.model_dump(by_alias=True, mode="json")),
        )
        worker.start()
        while True:
            scrape = store.get_scrape(scrape_id)
            if scrape is None:
                raise ScrapeDeletedError(f"scrape {scrape_id} was deleted while it ran")
            if scrape["Status"] in (ScrapeStatus.OK, ScrapeStatus.FAILED):
                break
            if not worker.is_alive():
                raise ScrapeWorkerDiedError(
                    f"the scrape worker stopped while scrape {scrape_id} was {scrape['Status']}"
                )
            time.sleep(_POLL_SECONDS)
        worker.shutdown(wait=1)
        crawler.start()
        _wait_for_websites(crawl, crawler, scrape_id, settings.max_seconds)
        unchecked = crawl.skip_pending_websites(scrape_id)
        if unchecked:
            logger.warning(
                "scrape %s: %s website(s) not checked: ran out of time",
                scrape_id,
                unchecked,
            )
        rows = store.read_rows(scrape_id)
        crawl.delete_scrape(scrape_id)
        store.delete_scrape(scrape_id)
        return ScrapeResult(
            scrape_id=scrape_id,
            status=scrape["Status"],
            note=scrape["Error"],
            rows=rows,
            blocked=bool(scrape["Progress"].get("blocked")),
        )
    finally:
        try:
            worker.shutdown(wait=1)
        except Exception:
            logger.exception("stopping the scrape worker failed")
        try:
            crawler.shutdown(wait=1)
        except Exception:
            logger.exception("stopping the crawler worker failed")
        finally:
            store.close()
