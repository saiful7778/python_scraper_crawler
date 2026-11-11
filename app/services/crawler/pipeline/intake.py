from dataclasses import dataclass

from app.helpers.http_fetcher.urls import hostname_of
from app.services.buffer.crawl_buffer import RedisCrawlBuffer
from app.services.buffer.crawl_queue import CrawlJob, CrawlQueue
from app.services.crawler.site.crawl import FetchFn
from app.services.crawler.site.scope import origin_of, registrable_domain
from app.utils.logger import get_logger

from .crawlability import check_origin

logger = get_logger(__name__)


@dataclass(frozen=True)
class IntakeResult:
    queued: int
    rejected: int
    skipped: int


class CrawlIntake:
    """Checks each website once and queues the crawlable ones; others are recorded as failed."""

    def __init__(self, queue: CrawlQueue, buffer: RedisCrawlBuffer, fetch: FetchFn):
        self._queue = queue
        self._buffer = buffer
        self._fetch = fetch

    def submit(self, websites: list[str]) -> IntakeResult:
        queued = rejected = skipped = 0
        for website in websites:
            try:
                origin = origin_of(website)
                host = hostname_of(origin)
            except Exception:
                logger.warning("website %r has no usable origin", website)
                skipped += 1
                continue
            if self._buffer.has_website(origin):
                logger.debug("website %s already crawled in this run; skipped", origin)
                skipped += 1
                continue
            check = check_origin(origin, self._fetch)
            if check.crawlable:
                job: CrawlJob = {"website": website, "origin": origin}
                if self._queue.enqueue(job):
                    logger.debug("website %s queued", website)
                    queued += 1
                else:
                    logger.debug("website %s already in the queue; skipped", website)
                    skipped += 1
                continue
            website_id = self._buffer.create_website(
                host=host,
                registrable_domain=registrable_domain(host),
                origin=origin,
            )
            self._buffer.fail_website(website_id, "not_crawlable", check.reason)
            logger.info("website %s not crawlable: %s", website, check.reason)
            rejected += 1
        return IntakeResult(queued=queued, rejected=rejected, skipped=skipped)
