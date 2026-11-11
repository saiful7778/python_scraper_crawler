import pytest

from app.services.buffer.crawl_buffer import RedisCrawlBuffer
from app.services.buffer.discard import discard_scrape
from app.services.buffer.keys import active_key, crawl_owners_key, rows_key
from app.services.buffer.known_keys import KnownKeys
from app.services.buffer.row_buffer import RowBuffer
from app.services.buffer.scrape_state import ScrapeState
from app.services.buffer.website_queue import WebsiteJob, WebsiteQueue

TTL = 3600


def seed(redis, scrape_id, host="a.de"):
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
        host=host,
        registrable_domain=host,
        origin=f"https://{host}",
    )
    run_id = crawl.create_run(website_id=website_id, requested_url="https://a.de", config={})
    crawl.finish_run(run_id, status="done", progress={}, error_code=None, error_message=None)
    return website_id, run_id


def test_discard_leaves_no_keys_for_the_scrape(fake_redis):
    seed(fake_redis, "s1")

    discard_scrape(fake_redis, "s1")

    assert fake_redis.keys("scrape:s1:*") == []
    assert fake_redis.sismember(active_key(), "s1") == 0


def test_discard_keeps_the_crawl_data_of_the_scrape(fake_redis):
    website_id, run_id = seed(fake_redis, "s1")

    discard_scrape(fake_redis, "s1")

    buffer = RedisCrawlBuffer(fake_redis, TTL)
    assert buffer.finished_website_ids() == [website_id]
    assert buffer.read_website(website_id).runs[0]["id"] == run_id
    assert set(fake_redis.hkeys(crawl_owners_key())) == {website_id, run_id}


def test_discard_keeps_the_data_of_other_scrapes(fake_redis):
    first, first_run = seed(fake_redis, "s1", host="a.de")
    second, second_run = seed(fake_redis, "s2", host="b.de")

    discard_scrape(fake_redis, "s1")

    assert len(fake_redis.keys("scrape:s2:*")) == 6
    assert set(fake_redis.hkeys(crawl_owners_key())) == {
        first,
        first_run,
        second,
        second_run,
    }
    assert sorted(RedisCrawlBuffer(fake_redis, TTL).finished_website_ids()) == sorted(
        [first, second]
    )


def test_discarding_an_unknown_scrape_is_harmless(fake_redis):
    discard_scrape(fake_redis, "never-existed")


def test_crawl_buffer_has_no_discard():
    assert not hasattr(RedisCrawlBuffer, "discard")


@pytest.mark.parametrize(
    "buffer_class",
    [ScrapeState, KnownKeys, RowBuffer, WebsiteQueue],
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
