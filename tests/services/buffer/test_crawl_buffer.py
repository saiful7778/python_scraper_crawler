from datetime import datetime

import pytest

from app.services.buffer.client import BufferFullError
from app.services.buffer.crawl_buffer import RedisCrawlBuffer
from app.services.buffer.crawl_types import LinkSeed, PageResult, PageSeed
from app.services.buffer.keys import crawl_finished_key, crawl_key, crawl_owners_key
from app.services.crawler.findings import Finding

HOME = "https://a.de/"
ABOUT = "https://a.de/about"
TTL = 3600


@pytest.fixture
def buffer(fake_redis):
    return RedisCrawlBuffer(fake_redis, TTL)


@pytest.fixture
def website_id(buffer):
    return buffer.create_website(
        host="a.de",
        registrable_domain="a.de",
        origin="https://a.de",
    )


@pytest.fixture
def run_id(buffer, website_id):
    return buffer.create_run(
        website_id=website_id, requested_url="https://a.de", config={"max_pages": 5}
    )


def _finish(buffer, run_id, status="done"):
    buffer.finish_run(
        run_id, status=status, progress={}, error_code=None, error_message=None
    )


def test_finished_website_is_readable_then_released(fake_redis):
    buffer = RedisCrawlBuffer(fake_redis, 3600)
    website_id = buffer.create_website(
        host="a.com",
        registrable_domain="a.com",
        origin="https://a.com",
    )
    buffer.fail_website(website_id, "dns", "no such host")
    assert buffer.finished_website_ids() == [website_id]
    assert buffer.read_website(website_id).website["status"] == "failed"
    buffer.release_website(website_id)
    assert buffer.finished_website_ids() == []


def test_create_website_stores_a_running_snapshot(buffer, website_id, fake_redis):
    website = buffer.read_website(website_id).website
    assert website["id"] == website_id
    assert website["host"] == "a.de"
    assert website["status"] == "running"
    assert website["error_code"] is None
    assert website["finished_at"] is None
    assert isinstance(website["created_at"], datetime)
    assert "scrape_id" not in website
    assert "lead_position" not in website
    assert "scrape_lead_id" not in website


def test_create_website_takes_no_scrape_or_lead_arguments(buffer):
    with pytest.raises(TypeError):
        buffer.create_website(
            scrape_id="s1",
            lead_position=0,
            scrape_lead_id=1,
            host="a.de",
            registrable_domain="a.de",
            origin="https://a.de",
        )


def test_create_website_id_is_derived_from_the_origin(buffer, website_id):
    again = buffer.create_website(
        host="a.de",
        registrable_domain="a.de",
        origin="https://a.de",
    )
    other = buffer.create_website(
        host="b.de",
        registrable_domain="b.de",
        origin="https://b.de",
    )
    assert again == website_id
    assert other != website_id


def test_create_website_again_for_the_same_origin_resets_it(buffer, website_id):
    buffer.fail_website(website_id, "dns", "no such host")
    again = buffer.create_website(
        host="a.de",
        registrable_domain="a.de",
        origin="https://a.de",
    )
    assert again == website_id
    assert buffer.finished_website_ids() == []
    assert buffer.read_website(website_id).website["status"] == "running"


def test_read_unknown_website_raises_lookup_error(buffer):
    with pytest.raises(LookupError):
        buffer.read_website("missing")


def test_add_pages_is_idempotent_per_run(buffer, website_id, run_id):
    seed = PageSeed("https://a.de/x", "https://a.de/x", 1, "link")
    first = buffer.add_pages(run_id, [seed])
    again = buffer.add_pages(run_id, [seed])
    assert first == again
    snapshot = buffer.read_website(website_id)
    assert len(snapshot.pages) == 1
    assert snapshot.pages[0]["status"] == "queued"


def test_finish_run_ends_the_run_and_its_website(buffer, website_id, run_id):
    assert buffer.finished_website_ids() == []
    buffer.finish_run(
        run_id,
        status="partial",
        progress={"pages": 3},
        error_code="timeout",
        error_message="slow",
    )
    snapshot = buffer.read_website(website_id)
    assert buffer.finished_website_ids() == [website_id]
    assert snapshot.website["status"] == "partial"
    assert snapshot.website["error_code"] == "timeout"
    assert isinstance(snapshot.website["finished_at"], datetime)
    (run,) = snapshot.runs
    assert run["id"] == run_id
    assert run["website_id"] == website_id
    assert run["status"] == "partial"
    assert run["progress"] == {"pages": 3}
    assert run["config"] == {"max_pages": 5}
    assert isinstance(run["started_at"], datetime)


def test_save_page_stores_the_result(buffer, website_id, run_id):
    page = buffer.add_pages(run_id, [PageSeed(HOME, HOME, 0, "start")])[HOME]
    buffer.save_page(
        page,
        PageResult(
            status="fetched", http_status=200, content_type="text/html", title="Home"
        ),
    )
    saved = buffer.read_website(website_id).pages[0]
    assert saved["status"] == "fetched"
    assert saved["http_status"] == 200
    assert saved["title"] == "Home"
    assert isinstance(saved["fetched_at"], datetime)


def test_save_page_stores_the_skip_reason(buffer, website_id, run_id):
    page = buffer.add_pages(run_id, [PageSeed(HOME, HOME, 0, "start")])[HOME]
    buffer.save_page(page, PageResult(status="skipped", skip_reason="not_html"))
    saved = buffer.read_website(website_id).pages[0]
    assert saved["skip_reason"] == "not_html"
    assert saved["fetched_at"] is None


def test_add_links_creates_unknown_targets_as_skipped(buffer, website_id, run_id):
    pages = buffer.add_pages(run_id, [PageSeed(HOME, HOME, 0, "start")])
    buffer.add_links(pages[HOME], [LinkSeed(ABOUT, "About", None, True)])
    snapshot = buffer.read_website(website_id)
    target = next(page for page in snapshot.pages if page["normalized_url"] == ABOUT)
    assert target["status"] == "skipped"
    assert target["skip_reason"] == "out_of_scope"
    (link,) = snapshot.links
    assert link["source_id"] == pages[HOME]
    assert link["target_id"] == target["id"]
    assert link["is_internal"] is True


def test_add_links_links_known_targets_once(buffer, website_id, run_id):
    pages = buffer.add_pages(
        run_id,
        [PageSeed(HOME, HOME, 0, "start"), PageSeed(ABOUT, ABOUT, 1, "link")],
    )
    link = LinkSeed(ABOUT, "About", None, True)
    buffer.add_links(pages[HOME], [link, link])
    buffer.add_links(pages[HOME], [link])
    snapshot = buffer.read_website(website_id)
    assert len(snapshot.pages) == 2
    assert len(snapshot.links) == 1
    assert snapshot.links[0]["target_id"] == pages[ABOUT]


def test_add_findings_and_events_are_in_the_snapshot(buffer, website_id, run_id):
    page = buffer.add_pages(run_id, [PageSeed(HOME, HOME, 0, "start")])[HOME]
    buffer.add_findings(
        website_id=website_id,
        run_id=run_id,
        page_id=page,
        findings=[Finding("contact", "email", "hi@a.de", {"source": "mailto"})],
    )
    buffer.add_event(run_id=run_id, step="fetch", level="info", message="one")
    buffer.add_event(
        run_id=run_id,
        step="parse",
        level="warning",
        message="two",
        page_id=page,
        data={"k": 1},
    )
    snapshot = buffer.read_website(website_id)
    (finding,) = snapshot.findings
    assert finding["website_id"] == website_id
    assert finding["run_id"] == run_id
    assert finding["page_id"] == page
    assert (finding["group"], finding["key"], finding["value"]) == (
        "contact",
        "email",
        "hi@a.de",
    )
    assert finding["extra"] == {"source": "mailto"}
    first, second = snapshot.events
    assert first["id"] < second["id"]
    assert (first["run_id"], first["page_id"], first["data"]) == (run_id, None, None)
    assert (second["page_id"], second["data"]) == (page, {"k": 1})


def test_finished_websites_are_listed_across_former_scrapes(buffer, website_id):
    other = buffer.create_website(
        host="b.de",
        registrable_domain="b.de",
        origin="https://b.de",
    )
    assert buffer.finished_website_ids() == []
    buffer.fail_website(other, "dns", "no such host")
    assert buffer.finished_website_ids() == [other]
    buffer.fail_website(website_id, "dns", "no such host")
    assert sorted(buffer.finished_website_ids()) == sorted([website_id, other])


def test_release_website_deletes_the_document_and_its_index(
    buffer, website_id, run_id, fake_redis
):
    page = buffer.add_pages(run_id, [PageSeed(HOME, HOME, 0, "start")])[HOME]
    _finish(buffer, run_id)
    buffer.release_website(website_id)
    assert fake_redis.exists(crawl_key(website_id)) == 0
    assert fake_redis.smembers(crawl_finished_key()) == set()
    assert fake_redis.hlen(crawl_owners_key()) == 0
    with pytest.raises(LookupError):
        buffer.read_website(website_id)
    with pytest.raises(LookupError):
        buffer.save_page(page, PageResult(status="fetched"))


def test_mark_interrupted_fails_running_websites_and_runs(buffer, website_id, run_id):
    done = buffer.create_website(
        host="b.de",
        registrable_domain="b.de",
        origin="https://b.de",
    )
    done_run = buffer.create_run(
        website_id=done, requested_url="https://b.de", config={}
    )
    _finish(buffer, done_run)
    buffer.mark_interrupted()
    assert sorted(buffer.finished_website_ids()) == sorted([website_id, done])
    interrupted = buffer.read_website(website_id)
    assert interrupted.website["status"] == "failed"
    assert interrupted.website["error_code"] == "interrupted"
    assert interrupted.runs[0]["status"] == "failed"
    assert interrupted.runs[0]["error_code"] == "interrupted"
    assert buffer.read_website(done).website["status"] == "done"


def test_writes_refresh_the_ttl_of_every_key_they_touch(buffer, website_id, fake_redis):
    keys = [
        crawl_key(website_id),
        crawl_finished_key(),
        crawl_owners_key(),
    ]
    run_id = buffer.create_run(
        website_id=website_id, requested_url="https://a.de", config={}
    )
    buffer.fail_website(website_id, "dns", "no such host")
    for key in keys:
        assert 0 < fake_redis.ttl(key) <= TTL
        fake_redis.expire(key, 10)
    buffer.add_event(run_id=run_id, step="x", level="info", message="m")
    for key in keys:
        assert 0 < fake_redis.ttl(key) <= TTL
        assert fake_redis.ttl(key) > 10


def test_full_redis_raises_buffer_full_error(fake_redis_full):
    buffer = RedisCrawlBuffer(fake_redis_full, TTL)
    with pytest.raises(BufferFullError):
        buffer.create_website(
            host="a.de",
            registrable_domain="a.de",
            origin="https://a.de",
        )


def test_skip_website_marks_it_skipped_and_finished(buffer, website_id):
    buffer.skip_website(website_id)
    assert buffer.read_website(website_id).website["status"] == "skipped"
    assert buffer.finished_website_ids() == [website_id]
