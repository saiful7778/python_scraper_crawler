from uuid import uuid4

import pytest

from app.models import (
    CrawlEventModel,
    CrawlFindingModel,
    CrawlLinkModel,
    CrawlPageModel,
    CrawlRunModel,
    CrawlWebsiteModel,
    LeadEmailModel,
    LeadModel,
    LeadSocialModel,
)
from app.services.buffer.crawl_buffer import RedisCrawlBuffer
from app.services.buffer.crawl_sink import _event_id, store_crawl
from app.services.buffer.crawl_types import LinkSeed, PageResult, PageSeed
from app.services.crawler.findings import Finding
from tests.models.conftest import db  # noqa: F401

HOME = "https://a.de/"
ABOUT = "https://a.de/about"


def finished_snapshot(fake_redis, host="www.a.de"):
    buffer = RedisCrawlBuffer(fake_redis, 3600)
    website_id = buffer.create_website(
        host=host,
        registrable_domain="a.de",
        origin=f"https://{host}",
    )
    run_id = buffer.create_run(
        website_id=website_id, requested_url=f"https://{host}", config={}
    )
    buffer.add_findings(
        website_id=website_id,
        run_id=run_id,
        page_id=None,
        findings=[
            Finding("contact", "email", "hi@a.de"),
            Finding("contact", "email", "sales@a.de"),
            Finding("social", "instagram", "https://instagram.com/a"),
            Finding("social", "instagram", "https://instagram.com/a_shop"),
            Finding("social", "facebook", "https://facebook.com/a"),
            Finding("tech", "cms", "wordpress"),
        ],
    )
    buffer.finish_run(
        run_id, status="done", progress={}, error_code=None, error_message=None
    )
    return buffer.read_website(website_id)


@pytest.mark.usefixtures("db")
async def test_crawl_is_stored_and_linked_to_the_lead_of_its_domain(fake_redis):
    lead = await LeadModel.create(title="A", website="https://a.de", website_domain="a.de")

    await store_crawl(finished_snapshot(fake_redis))

    website = await CrawlWebsiteModel.get()
    assert website.lead_id == lead.id
    assert website.cms == "wordpress"
    assert await CrawlRunModel.all().count() == 1
    assert await CrawlFindingModel.all().count() == 6


@pytest.mark.usefixtures("db")
async def test_emails_and_one_social_per_platform_reach_the_lead(fake_redis):
    lead = await LeadModel.create(title="A", website="https://a.de", website_domain="a.de")

    await store_crawl(finished_snapshot(fake_redis))

    emails = await LeadEmailModel.filter(lead_id=lead.id).values_list("email", flat=True)
    assert sorted(emails) == ["hi@a.de", "sales@a.de"]
    socials = dict(
        await LeadSocialModel.filter(lead_id=lead.id).values_list("platform", "url")
    )
    assert socials == {
        "instagram": "https://instagram.com/a",
        "facebook": "https://facebook.com/a",
    }


@pytest.mark.usefixtures("db")
async def test_storing_a_crawl_twice_changes_nothing(fake_redis):
    lead = await LeadModel.create(title="A", website="https://a.de", website_domain="a.de")
    snapshot = finished_snapshot(fake_redis)

    await store_crawl(snapshot)
    await store_crawl(snapshot)

    assert await CrawlWebsiteModel.all().count() == 1
    assert await LeadEmailModel.filter(lead_id=lead.id).count() == 2


@pytest.mark.usefixtures("db")
async def test_crawl_without_a_lead_is_stored_with_no_lead(fake_redis):
    await store_crawl(finished_snapshot(fake_redis))
    website = await CrawlWebsiteModel.get()
    assert website.lead_id is None
    assert await LeadEmailModel.all().count() == 0


@pytest.mark.usefixtures("db")
async def test_crawl_stored_before_its_lead_links_on_the_next_store(fake_redis):
    snapshot = finished_snapshot(fake_redis)
    await store_crawl(snapshot)
    lead = await LeadModel.create(title="A", website="https://a.de", website_domain="a.de")
    await store_crawl(snapshot)
    assert (await CrawlWebsiteModel.get()).lead_id == lead.id
    assert await LeadEmailModel.filter(lead_id=lead.id).count() == 2


def _crawl_with_pages(fake_redis):
    buffer = RedisCrawlBuffer(fake_redis, 3600)
    website_id = buffer.create_website(
        host="www.a.de",
        registrable_domain="a.de",
        origin="https://www.a.de",
    )
    run_id = buffer.create_run(
        website_id=website_id, requested_url="https://www.a.de", config={}
    )
    pages = buffer.add_pages(
        run_id,
        [PageSeed(HOME, HOME, 0, "seed"), PageSeed(ABOUT, ABOUT, 1, "link")],
    )
    buffer.save_page(pages[HOME], PageResult(status="fetched", http_status=200))
    buffer.save_page(pages[ABOUT], PageResult(status="fetched", http_status=200))
    buffer.add_links(pages[HOME], [LinkSeed(ABOUT, "About", None, True)])
    buffer.add_event(run_id=run_id, step="crawl", level="info", message="started")
    buffer.finish_run(
        run_id, status="done", progress={"pages": 2}, error_code=None, error_message=None
    )
    return buffer.read_website(website_id)


@pytest.mark.usefixtures("db")
async def test_every_table_of_the_crawl_is_stored(fake_redis):
    await LeadModel.create(title="A", website="https://a.de", website_domain="a.de")
    snapshot = _crawl_with_pages(fake_redis)

    await store_crawl(snapshot)

    assert await CrawlWebsiteModel.all().count() == 1
    assert await CrawlRunModel.all().count() == 1
    assert await CrawlPageModel.all().count() == len(snapshot.pages) == 2
    assert await CrawlLinkModel.all().count() == len(snapshot.links) == 1
    assert await CrawlEventModel.all().count() == len(snapshot.events) == 1


@pytest.mark.usefixtures("db")
async def test_events_are_stored_under_ids_derived_from_their_run(fake_redis):
    await LeadModel.create(title="A", website="https://a.de", website_domain="a.de")
    snapshot = _crawl_with_pages(fake_redis)
    run_id = str(snapshot.runs[0]["id"])

    await store_crawl(snapshot)

    event = await CrawlEventModel.get()
    assert str(event.id) == str(_event_id(run_id, snapshot.events[0]["id"]))


@pytest.mark.usefixtures("db")
async def test_a_changed_website_is_updated_by_the_next_store(fake_redis):
    await LeadModel.create(title="A", website="https://a.de", website_domain="a.de")
    snapshot = finished_snapshot(fake_redis)
    await store_crawl(snapshot)

    snapshot.website = {**snapshot.website, "status": "failed"}
    await store_crawl(snapshot)

    assert (await CrawlWebsiteModel.get()).status == "failed"


def test_event_ids_are_stable_per_run_and_differ_across_runs():
    assert _event_id("run-a", 1) == _event_id("run-a", 1)
    assert _event_id("run-a", 1) != _event_id("run-b", 1)
    assert _event_id("run-a", 1) != _event_id("run-a", 2)


@pytest.mark.usefixtures("db")
async def test_a_pre_migration_website_row_is_reused_for_its_origin(fake_redis):
    lead = await LeadModel.create(title="A", website="https://a.de", website_domain="a.de")
    old_id = uuid4()
    await CrawlWebsiteModel.create(
        id=old_id,
        lead=lead,
        host="www.a.de",
        registrable_domain="a.de",
        origin="https://www.a.de",
        status="done",
    )
    snapshot = finished_snapshot(fake_redis)
    assert snapshot.website["id"] != str(old_id)

    await store_crawl(snapshot)

    website = await CrawlWebsiteModel.get(origin="https://www.a.de")
    assert await CrawlWebsiteModel.all().count() == 1
    assert str(website.id) == str(old_id)
    assert website.status == snapshot.website["status"]
    assert await CrawlRunModel.filter(website_id=old_id).count() == 1
    assert await CrawlFindingModel.filter(website_id=old_id).count() == 6
    assert await CrawlRunModel.filter(website_id=snapshot.website["id"]).count() == 0
