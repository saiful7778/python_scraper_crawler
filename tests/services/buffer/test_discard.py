import pytest

from app.services.buffer.crawl_buffer import RedisCrawlBuffer
from app.services.buffer.discard import discard_scrape
from app.services.buffer.keys import active_key, crawl_owners_key, rows_key
from app.services.buffer.known_keys import KnownKeys
from app.services.buffer.row_buffer import RowBuffer
from app.services.buffer.scrape_state import ScrapeState
from app.services.buffer.website_queue import WebsiteJob, WebsiteQueue

TTL = 3600


def seed(redis, scrape_id):
    ScrapeState(redis, TTL).create(scrape_id, {"email": True})
    ScrapeState(redis, TTL).save_progress(scrape_id, {"done": 1})
    RowBuffer(redis, TTL).append(scrape_id, [{"title": "A"}])
    KnownKeys(redis, TTL).load(scrape_id, ["title:A"])
    queue = WebsiteQueue(redis, TTL)
    job = WebsiteJob(
        scrape_id=scrape_id,
        position=0,
        row_id="r",
        website="https://a.de",
        origin="https://a.de",
    )
    queue.enqueue(scrape_id, job)
    queue.claim(scrape_id)
    queue.enqueue(scrape_id, {**job, "origin": "https://b.de"})
    crawl = RedisCrawlBuffer(redis, TTL)
    website_id = crawl.create_website(
        scrape_id=scrape_id,
        lead_position=0,
        scrape_lead_id=0,
        host="a.de",
        registrable_domain="a.de",
        origin="https://a.de",
    )
    run_id = crawl.create_run(website_id=website_id, requested_url="https://a.de", config={})
    crawl.finish_run(run_id, status="done", progress={}, error_code=None, error_message=None)
    return website_id, run_id


def test_discard_leaves_no_keys_for_the_scrape(fake_redis):
    seed(fake_redis, "s1")

    discard_scrape(fake_redis, "s1")

    assert fake_redis.keys("scrape:s1:*") == []
    assert fake_redis.sismember(active_key(), "s1") == 0
    assert fake_redis.hlen(crawl_owners_key()) == 0


def test_discard_keeps_the_data_of_other_scrapes(fake_redis):
    seed(fake_redis, "s1")
    website_id, run_id = seed(fake_redis, "s2")

    discard_scrape(fake_redis, "s1")

    assert fake_redis.keys("scrape:s1:*") == []
    assert len(fake_redis.keys("scrape:s2:*")) == 8
    assert set(fake_redis.hkeys(crawl_owners_key())) == {website_id, run_id}
    assert RedisCrawlBuffer(fake_redis, TTL).finished_website_ids() == [website_id]


def test_discarding_an_unknown_scrape_is_harmless(fake_redis):
    discard_scrape(fake_redis, "never-existed")


@pytest.mark.parametrize(
    "buffer_class",
    [ScrapeState, KnownKeys, RowBuffer, WebsiteQueue, RedisCrawlBuffer],
)
def test_discard_scrape_calls_every_buffer_discard(
    fake_redis, monkeypatch, buffer_class
):
    calls = []
    monkeypatch.setattr(
        buffer_class, "discard", lambda self, scrape_id: calls.append(scrape_id)
    )

    discard_scrape(fake_redis, "s1")

    assert calls == ["s1"]


def test_writes_refresh_the_expiry(fake_redis):
    RowBuffer(fake_redis, 3600).append("s1", [{"title": "A"}])
    assert 0 < fake_redis.ttl(rows_key("s1")) <= 3600
