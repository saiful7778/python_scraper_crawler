"""Runs the crawl stage on the websites of scraped leads and stores the finished crawls."""

from dataclasses import dataclass

from redis import Redis

from app.helpers.http_fetcher import WebPageFetcher
from app.repositories import GoogleMapProfileRepository, LeadRepository
from app.services.buffer.crawl_buffer import RedisCrawlBuffer
from app.services.buffer.crawl_queue import CrawlQueue
from app.services.buffer.crawl_sink import store_crawl
from app.services.buffer.flusher import Flusher
from app.services.buffer.row_buffer import RowBuffer
from app.services.buffer.scrape_state import ScrapeState
from app.services.google_map_scraper.scrape.scrape_limits import BUFFER_TTL_SECONDS
from app.services.google_map_scraper.scrape.scrape_types import LeadRow
from config.tortoise import TortoiseConfig

from .intake import CrawlIntake
from .stage import CRAWL_OPTIONS, CrawlStage, site_fetch

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
    rows: list[LeadRow], *, redis: Redis, postgres: TortoiseConfig
) -> CrawlSummary:
    """Check the websites of ``rows``, crawl the crawlable ones and store each finished crawl in PostgreSQL.

    Crawls that an earlier run left unfinished are queued again first. The
    flusher opens its own Tortoise connection, the same way the scrape does.
    A stop (Ctrl-C) leaves unfinished crawls in Redis for the next run.
    """
    websites = list(dict.fromkeys(row["website"] for row in rows if row.get("website")))
    queue = CrawlQueue(redis)
    buffer = RedisCrawlBuffer(redis, BUFFER_TTL_SECONDS)
    fetch = site_fetch(WebPageFetcher())

    queue.requeue_processing()
    intake = CrawlIntake(queue, buffer, fetch).submit(websites)

    flusher = Flusher(
        RowBuffer(redis, BUFFER_TTL_SECONDS),
        ScrapeState(redis, BUFFER_TTL_SECONDS),
        save_row=GoogleMapProfileRepository(LeadRepository()).save_row,
        crawl_sink=store_crawl,
        config=postgres,
        crawl_buffer=buffer,
    )
    stage = CrawlStage(queue, buffer, fetch, CRAWL_THREADS, CRAWL_OPTIONS)
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
