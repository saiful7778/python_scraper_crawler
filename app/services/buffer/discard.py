from redis import Redis

from app.services.buffer.crawl_buffer import RedisCrawlBuffer
from app.services.buffer.known_keys import KnownKeys
from app.services.buffer.row_buffer import RowBuffer
from app.services.buffer.scrape_state import ScrapeState
from app.services.buffer.website_queue import WebsiteQueue


def discard_scrape(redis: Redis, scrape_id: str) -> None:
    """Delete every Redis key of a scrape, and its entry in the active set."""
    RedisCrawlBuffer(redis, 0).discard(scrape_id)
    WebsiteQueue(redis, 0).discard(scrape_id)
    KnownKeys(redis, 0).discard(scrape_id)
    RowBuffer(redis, 0).discard(scrape_id)
    ScrapeState(redis, 0).discard(scrape_id)
