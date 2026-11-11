import pytest

from app.services.buffer.crawl_buffer import RedisCrawlBuffer
from app.services.buffer.flusher import Flusher
from app.services.buffer.keys import crawl_key
from app.services.buffer.row_buffer import RowBuffer
from app.services.buffer.scrape_state import ScrapeState
from app.services.crawler.findings import Finding

TTL = 3600


class Sink:
    def __init__(self, fail=False):
        self.snapshots = []
        self.fail = fail

    async def __call__(self, snapshot):
        if self.fail:
            raise RuntimeError("database is down")
        self.snapshots.append(snapshot)


@pytest.fixture
def crawl(fake_redis):
    return RedisCrawlBuffer(fake_redis, TTL)


@pytest.fixture
def state(fake_redis):
    return ScrapeState(fake_redis, TTL)


@pytest.fixture
def make_flusher(fake_redis, state, crawl):
    flushers = []

    def make(sink):
        flusher = Flusher(
            RowBuffer(fake_redis, TTL),
            state,
            save_row=_no_rows,
            crawl_sink=sink,
            crawl_buffer=crawl,
        )
        flushers.append(flusher)
        return flusher

    yield make
    for flusher in flushers:
        flusher.shutdown()


async def _no_rows(row, scrape_id):
    raise AssertionError("no rows were buffered")


def finish_website(crawl, scrape_id="s1", position=0):
    website_id = crawl.create_website(
        scrape_id=scrape_id,
        lead_position=position,
        scrape_lead_id=position,
        host="a.de",
        registrable_domain="a.de",
        origin="https://a.de",
    )
    run_id = crawl.create_run(website_id=website_id, requested_url="https://a.de", config={})
    crawl.add_findings(
        website_id=website_id,
        run_id=run_id,
        page_id=None,
        findings=[Finding("contact", "email", "hi@a.de")],
    )
    crawl.finish_run(
        run_id, status="done", progress={}, error_code=None, error_message=None
    )
    return website_id


def test_finished_websites_are_stored_then_released(
    fake_redis, state, crawl, make_flusher
):
    state.create("s1", {})
    website_id = finish_website(crawl)
    sink = Sink()

    make_flusher(sink).flush_once()

    assert [s.website["id"] for s in sink.snapshots] == [website_id]
    assert [f["value"] for f in sink.snapshots[0].findings] == ["hi@a.de"]
    assert crawl.finished_website_ids() == []
    assert fake_redis.get(crawl_key("s1", website_id)) is None


def test_a_failed_store_keeps_the_website_for_the_next_tick(
    state, crawl, make_flusher
):
    state.create("s1", {})
    website_id = finish_website(crawl)
    sink = Sink(fail=True)
    flusher = make_flusher(sink)

    flusher.flush_once()
    assert crawl.finished_website_ids() == [website_id]

    sink.fail = False
    flusher.flush_once()
    assert crawl.finished_website_ids() == []
    assert len(sink.snapshots) == 1


def test_an_expired_document_is_skipped_and_the_rest_are_stored(
    fake_redis, state, crawl, make_flusher
):
    state.create("s1", {})
    expired = finish_website(crawl, position=0)
    kept = finish_website(crawl, position=1)
    fake_redis.delete(crawl_key("s1", expired))
    sink = Sink()

    make_flusher(sink).flush_once()

    assert [s.website["id"] for s in sink.snapshots] == [kept]
    assert crawl.finished_website_ids() == []


def test_one_failing_website_does_not_block_the_others(state, crawl, make_flusher):
    state.create("s1", {})
    first = finish_website(crawl, position=0)
    second = finish_website(crawl, position=1)

    class FailsFirst(Sink):
        async def __call__(self, snapshot):
            if snapshot.website["id"] == first:
                raise RuntimeError("bad document")
            await super().__call__(snapshot)

    sink = FailsFirst()
    make_flusher(sink).flush_once()

    assert [s.website["id"] for s in sink.snapshots] == [second]
    assert crawl.finished_website_ids() == [first]


def test_the_shutdown_tick_stores_what_finished_last(state, crawl, make_flusher):
    state.create("s1", {})
    finish_website(crawl)
    sink = Sink()
    flusher = make_flusher(sink)
    flusher.start()
    flusher.shutdown(wait=5)
    assert not flusher.is_alive()
    assert len(sink.snapshots) == 1
