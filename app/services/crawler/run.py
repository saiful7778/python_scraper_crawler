"""Runs the crawl on the websites of scraped leads, or on one website, and stores each finished crawl."""

from dataclasses import dataclass

from redis import Redis

from app.helpers.http_fetcher import WebPageFetcher
from app.helpers.http_fetcher.urls import hostname_of
from app.repositories import GoogleMapProfileRepository, LeadRepository
from app.services.buffer.crawl_buffer import RedisCrawlBuffer, _website_id_of
from app.services.buffer.crawl_queue import CrawlQueue
from app.services.buffer.crawl_sink import store_crawl
from app.services.buffer.flusher import Flusher
from app.services.buffer.row_buffer import RowBuffer
from app.services.buffer.scrape_state import ScrapeState
from app.services.crawler.site.crawl import CrawlOptions, FetchFn
from app.services.crawler.site.scope import origin_of, registrable_domain
from app.services.crawler.worker import check_website
from app.services.google_map_scraper.scrape.scrape_limits import BUFFER_TTL_SECONDS
from app.services.google_map_scraper.scrape.scrape_types import LeadRow
from app.utils.errors import ScraperError
from config.tortoise import TortoiseConfig

from .pipeline.crawlability import check_origin
from .pipeline.intake import CrawlIntake
from .pipeline.stage import CRAWL_OPTIONS, CrawlStage, site_fetch

CRAWL_THREADS = 4
#: Seconds the threads get to stop after their current website.
_STOP_SECONDS = 10
#: Seconds the flusher gets for its last flush of finished crawls.
_FINAL_FLUSH_SECONDS = 60


@dataclass(frozen=True)
class CrawlSummary:
    queued: int
    rejected: int
    skipped: int
    checked: int


def run_crawl_stage(
    rows: list[LeadRow],
    *,
    redis: Redis,
    postgres: TortoiseConfig,
    options: CrawlOptions = CRAWL_OPTIONS,
    fetch: FetchFn | None = None,
) -> CrawlSummary:
    """Check the websites of ``rows``, crawl the crawlable ones and store each finished crawl in PostgreSQL.

    Crawls that an earlier run left unfinished are queued again first. The
    flusher opens its own Tortoise connection, the same way the scrape does.
    A stop (Ctrl-C) leaves unfinished crawls in Redis for the next run.
    """
    websites = list(dict.fromkeys(row["website"] for row in rows if row.get("website")))
    queue = CrawlQueue(redis)
    buffer = RedisCrawlBuffer(redis, BUFFER_TTL_SECONDS)
    fetch = fetch or site_fetch(WebPageFetcher())

    queue.requeue_processing()
    intake = CrawlIntake(queue, buffer, fetch).submit(websites)

    flusher = _flusher(redis, buffer, postgres)
    stage = CrawlStage(queue, buffer, fetch, CRAWL_THREADS, options)
    flusher.start()
    try:
        stage.run_until_empty()
    finally:
        stage.shutdown(wait=_STOP_SECONDS)
        flusher.shutdown(wait=_FINAL_FLUSH_SECONDS)

    return CrawlSummary(
        queued=intake.queued,
        rejected=intake.rejected,
        skipped=intake.skipped,
        checked=intake.queued + intake.rejected,
    )


def crawl_one(
    website: str,
    *,
    redis: Redis,
    postgres: TortoiseConfig,
    options: CrawlOptions,
    finish_incomplete: bool = False,
    fetch: FetchFn | None = None,
) -> CrawlSummary:
    """Crawl one website and store it, linked to its lead when one matches.

    With ``finish_incomplete`` the shared queue is drained as well, the same
    way the scrape does. Without it the shared queue is not touched.
    """
    if finish_incomplete:
        return run_crawl_stage(
            [{"website": website}],
            redis=redis,
            postgres=postgres,
            options=options,
            fetch=fetch,
        )

    fetch = fetch or site_fetch(WebPageFetcher())
    try:
        origin = origin_of(website)
        host = hostname_of(origin)
    except Exception:
        return CrawlSummary(queued=0, rejected=0, skipped=1, checked=0)

    buffer = RedisCrawlBuffer(redis, BUFFER_TTL_SECONDS)
    website_id = _website_id_of(origin)
    flusher = _flusher(redis, buffer, postgres)
    flusher.start()
    completed = False
    try:
        summary = _check_directly(buffer, fetch, website, origin, host, options)
        completed = True
    finally:
        flusher.shutdown(wait=_FINAL_FLUSH_SECONDS)
        if not completed and website_id not in buffer.finished_website_ids():
            buffer.release_website(website_id)
    if website_id in buffer.finished_website_ids():
        raise ScraperError(
            f"the crawl of {website} is not saved in PostgreSQL; it stays in Redis "
            "and the next run saves it"
        )
    return summary


def _check_directly(
    buffer: RedisCrawlBuffer,
    fetch: FetchFn,
    website: str,
    origin: str,
    host: str,
    options: CrawlOptions,
) -> CrawlSummary:
    check = check_origin(origin, fetch)
    if not check.crawlable:
        website_id = buffer.create_website(
            host=host,
            registrable_domain=registrable_domain(host),
            origin=origin,
        )
        buffer.fail_website(website_id, "not_crawlable", check.reason)
        return CrawlSummary(queued=0, rejected=1, skipped=0, checked=1)
    check_website(buffer, fetch, {"website": website, "origin": origin}, options)
    return CrawlSummary(queued=0, rejected=0, skipped=0, checked=1)


def _flusher(
    redis: Redis, buffer: RedisCrawlBuffer, postgres: TortoiseConfig
) -> Flusher:
    return Flusher(
        RowBuffer(redis, BUFFER_TTL_SECONDS),
        ScrapeState(redis, BUFFER_TTL_SECONDS),
        save_row=GoogleMapProfileRepository(LeadRepository()).save_row,
        crawl_sink=store_crawl,
        config=postgres,
        crawl_buffer=buffer,
    )
