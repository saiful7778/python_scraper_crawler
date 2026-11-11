import fakeredis
import pytest

from app.services.buffer.crawl_buffer import RedisCrawlBuffer
from app.services.buffer.website_queue import WebsiteJob
from app.services.crawler.worker import skip_website

TTL = 3600


@pytest.fixture
def fake_redis():
    return fakeredis.FakeRedis(decode_responses=True)


def test_skip_website_records_the_queued_website_as_skipped(fake_redis):
    buffer = RedisCrawlBuffer(fake_redis, TTL)
    job: WebsiteJob = {
        "scrape_id": "s1",
        "position": 3,
        "row_id": "4",
        "website": "https://bakery.example/about",
        "origin": "https://bakery.example",
    }
    skip_website(buffer, job)

    (website_id,) = buffer.finished_website_ids()
    snapshot = buffer.read_website(website_id)
    assert snapshot.website["status"] == "skipped"
    assert snapshot.website["origin"] == "https://bakery.example"
    assert snapshot.website["lead_position"] == 3
