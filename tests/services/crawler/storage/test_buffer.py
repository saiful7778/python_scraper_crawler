import pytest

from app.services.crawler.findings import Finding
from app.services.crawler.storage.buffer import (
    CrawlBuffer,
    LinkSeed,
    PageResult,
    PageSeed,
)
from app.services.crawler.storage.crawl_store import CrawlStore
from app.services.crawler.storage.models import CrawlWebsiteModel
from app.services.google_map_scraper.scrape.scrape_models import ScrapeLeadModel
from app.services.google_map_scraper.scrape.scrape_settings import ScrapeSettings
from app.services.google_map_scraper.scrape.scrape_store import ScrapeStore
from app.services.google_map_scraper.scrape.scrape_types import LeadRow

HOME = "https://a.de/"
ABOUT = "https://a.de/about"


def _lead(**extra: str) -> LeadRow:
    row: dict[str, object] = {key: "" for key in LeadRow.__required_keys__}
    row["latitude"] = None
    row["longitude"] = None
    row["title"] = "Bakery"
    row.update(extra)
    return row  # type: ignore[return-value]


async def _first_lead_id(scrape_id: str) -> int:
    lead = await ScrapeLeadModel.filter(scrape_id=scrape_id).first()
    assert lead is not None
    return lead.id


@pytest.fixture
def store(tmp_path):
    scrape_store = ScrapeStore(str(tmp_path))
    yield scrape_store
    scrape_store.close()


@pytest.fixture
def buffer(store):
    return CrawlBuffer(CrawlStore(store))


@pytest.fixture
def website_id(store, buffer):
    settings = ScrapeSettings(keywords=("bakery",))
    scrape_id = store.create_scrape(
        "t", settings.model_dump(by_alias=True, mode="json")
    )
    store.append_rows(scrape_id, [_lead(website="https://a.de")])
    lead_id = store.run_async(_first_lead_id(scrape_id))
    return buffer.create_website(
        scrape_id=scrape_id,
        lead_position=0,
        scrape_lead_id=lead_id,
        host="a.de",
        registrable_domain="a.de",
        origin="https://a.de",
    )


@pytest.fixture
def run_id(buffer, website_id):
    return buffer.create_run(
        website_id=website_id, requested_url="https://a.de", config={"max_pages": 5}
    )


def test_add_pages_is_idempotent_per_run(buffer, website_id, run_id):
    first = buffer.add_pages(
        run_id, [PageSeed("https://a.de/x", "https://a.de/x", 1, "link")]
    )
    again = buffer.add_pages(
        run_id, [PageSeed("https://a.de/x", "https://a.de/x", 1, "link")]
    )
    assert first == again
    assert len(buffer.read_website(website_id).pages) == 1


def test_only_finished_websites_are_listed_as_ready(buffer, website_id, run_id):
    assert buffer.finished_website_ids() == []
    buffer.finish_run(
        run_id, status="done", progress={}, error_code=None, error_message=None
    )
    assert buffer.finished_website_ids() == [website_id]


def test_release_website_drops_its_crawl_data_but_keeps_the_row(
    buffer, website_id, run_id
):
    page = buffer.add_pages(run_id, [PageSeed(HOME, HOME, 0, "start")])
    buffer.add_findings(
        website_id=website_id,
        run_id=run_id,
        page_id=page[HOME],
        findings=[Finding("contact", "email", "hi@a.de")],
    )
    buffer.finish_run(
        run_id, status="done", progress={}, error_code=None, error_message=None
    )
    buffer.release_website(website_id)
    assert buffer.finished_website_ids() == []
    snapshot = buffer.read_website(website_id)
    assert snapshot.runs == []
    assert snapshot.pages == []
    assert snapshot.findings == []


def test_save_page_stores_the_result_and_the_html(buffer, website_id, run_id):
    page = buffer.add_pages(run_id, [PageSeed(HOME, HOME, 0, "start")])[HOME]
    buffer.save_page(
        page,
        PageResult(
            status="fetched",
            http_status=200,
            content_type="text/html",
            title="Home",
            html="<html></html>",
        ),
    )
    snapshot = buffer.read_website(website_id)
    saved = snapshot.pages[0]
    assert saved["status"] == "fetched"
    assert saved["http_status"] == 200
    assert saved["title"] == "Home"
    assert saved["fetched_at"] is not None
    assert [row["html"] for row in snapshot.raw_html] == ["<html></html>"]
    assert snapshot.raw_html[0]["page_id"] == saved["id"]


def test_save_page_without_html_stores_no_raw_html(buffer, website_id, run_id):
    page = buffer.add_pages(run_id, [PageSeed(HOME, HOME, 0, "start")])[HOME]
    buffer.save_page(page, PageResult(status="skipped", skip_reason="not_html"))
    snapshot = buffer.read_website(website_id)
    assert snapshot.pages[0]["skip_reason"] == "not_html"
    assert snapshot.raw_html == []


def test_add_links_creates_unknown_targets_as_skipped(buffer, website_id, run_id):
    pages = buffer.add_pages(run_id, [PageSeed(HOME, HOME, 0, "start")])
    buffer.add_links(
        pages[HOME],
        [
            LinkSeed(HOME, None, None, True),
            LinkSeed(ABOUT, "About", "nofollow", True),
            LinkSeed(ABOUT, "About again", None, True),
        ],
    )
    snapshot = buffer.read_website(website_id)
    by_url = {p["normalized_url"]: p for p in snapshot.pages}
    assert by_url[ABOUT]["status"] == "skipped"
    assert by_url[ABOUT]["depth"] == 1
    assert len(snapshot.links) == 2
    about_link = next(
        link for link in snapshot.links if link["target_id"] == by_url[ABOUT]["id"]
    )
    assert about_link["source_id"] == by_url[HOME]["id"]
    assert about_link["anchor_text"] == "About"


def test_findings_and_events_are_read_back(buffer, website_id, run_id):
    page = buffer.add_pages(run_id, [PageSeed(HOME, HOME, 0, "start")])[HOME]
    buffer.add_findings(
        website_id=website_id,
        run_id=run_id,
        page_id=None,
        findings=[Finding("tech", "cms", "wordpress", {"version": "6"})],
    )
    buffer.add_event(
        run_id=run_id,
        step="page_fetch",
        level="error",
        message="timeout",
        page_id=page,
        data={"attempt": 2},
    )
    snapshot = buffer.read_website(website_id)
    assert snapshot.findings[0]["key"] == "cms"
    assert snapshot.findings[0]["page_id"] is None
    assert snapshot.findings[0]["extra"] == {"version": "6"}
    assert snapshot.events[0]["step"] == "page_fetch"
    assert snapshot.events[0]["data"] == {"attempt": 2}


def test_finish_run_sets_run_and_website_status(buffer, website_id, run_id):
    buffer.finish_run(
        run_id,
        status="partial",
        progress={"fetched": 3},
        error_code="timeout",
        error_message="took too long",
    )
    snapshot = buffer.read_website(website_id)
    assert snapshot.website["status"] == "partial"
    assert snapshot.website["finished_at"] is not None
    assert snapshot.runs[0]["status"] == "partial"
    assert snapshot.runs[0]["progress"] == {"fetched": 3}
    assert snapshot.runs[0]["error_code"] == "timeout"
    assert snapshot.runs[0]["config"] == {"max_pages": 5}


def test_fail_website_marks_it_failed_and_finished(buffer, website_id):
    buffer.fail_website(website_id, "run_not_started", "no run was created")
    snapshot = buffer.read_website(website_id)
    assert snapshot.website["status"] == "failed"
    assert snapshot.website["error_code"] == "run_not_started"
    assert snapshot.website["error_message"] == "no run was created"
    assert snapshot.website["finished_at"] is not None
    assert buffer.finished_website_ids() == [website_id]


def test_mark_interrupted_fails_running_websites_and_runs(buffer, website_id, run_id):
    buffer.mark_interrupted()
    snapshot = buffer.read_website(website_id)
    assert snapshot.website["status"] == "failed"
    assert snapshot.website["error_code"] == "interrupted"
    assert snapshot.website["finished_at"] is not None
    assert snapshot.runs[0]["status"] == "failed"
    assert snapshot.runs[0]["error_code"] == "interrupted"
    assert website_id in buffer.finished_website_ids()


async def _website_link(website_id: str) -> dict:
    website = await CrawlWebsiteModel.get(id=website_id)
    return {
        "scrape_id": website.scrape_id,
        "lead_position": website.lead_position,
        "scrape_lead_id": website.scrape_lead_id,
    }


async def _website_count(scrape_id: str) -> int:
    return await CrawlWebsiteModel.filter(scrape_id=scrape_id).count()


def test_replace_rows_keeps_the_crawl_buffer(store, buffer):
    settings = ScrapeSettings(keywords=("bakery",))
    scrape_id = store.create_scrape(
        "t", settings.model_dump(by_alias=True, mode="json")
    )
    store.append_rows(scrape_id, [_lead(website="https://a.de")])
    lead_id = store.run_async(_first_lead_id(scrape_id))
    website_id = buffer.create_website(
        scrape_id=scrape_id,
        lead_position=0,
        scrape_lead_id=lead_id,
        host="a.de",
        registrable_domain="a.de",
        origin="https://a.de",
    )
    store.replace_rows(scrape_id, [_lead(website="https://a.de", title="Bakery 2")])
    link = store.run_async(_website_link(website_id))
    assert link["scrape_id"] == scrape_id
    assert link["lead_position"] == 0
    assert link["scrape_lead_id"] is None


def test_delete_scrape_removes_its_crawl_buffer(store, buffer):
    settings = ScrapeSettings(keywords=("bakery",))
    scrape_id = store.create_scrape(
        "t", settings.model_dump(by_alias=True, mode="json")
    )
    store.append_rows(scrape_id, [_lead(website="https://a.de")])
    lead_id = store.run_async(_first_lead_id(scrape_id))
    buffer.create_website(
        scrape_id=scrape_id,
        lead_position=0,
        scrape_lead_id=lead_id,
        host="a.de",
        registrable_domain="a.de",
        origin="https://a.de",
    )
    assert store.run_async(_website_count(scrape_id)) == 1
    assert store.delete_scrape(scrape_id)
    assert store.run_async(_website_count(scrape_id)) == 0
