import time
from urllib.parse import urlsplit

import pytest

from app.helpers.http_fetcher.models import FetchResult, build_result
from app.services.crawler.site.crawl import WebsiteCrawl
from app.services.crawler.storage.buffer import CrawlBuffer
from app.services.crawler.storage.crawl_store import CrawlStore
from app.services.crawler.storage.postgres_sink import PostgresSink
from app.services.crawler.worker import CrawlerWorker
from app.services.google_map_scraper.scrape.scrape_store import ScrapeStore
from app.services.google_map_scraper.scrape.scrape_types import LeadRow, SettingsData

PAGE = '<a href="mailto:hi@bakery.studio.de">mail</a>'


def _settings() -> SettingsData:
    return {  # type: ignore[return-value]
        "keywords": ["bakery"],
        "lang": "de",
        "max_time": 600,
        "email": True,
        "socials": False,
    }


def _lead(title: str, **extra: str) -> LeadRow:
    row: dict[str, object] = {key: "" for key in LeadRow.__required_keys__}
    row["latitude"] = None
    row["longitude"] = None
    row["title"] = title
    row.update(extra)
    return row  # type: ignore[return-value]


class _CountingFetch:
    def __init__(self, page: str):
        self._page = page
        self.homepage_calls = 0

    def __call__(self, url: str) -> FetchResult:
        if urlsplit(url).path in ("", "/"):
            self.homepage_calls += 1
            return build_result(
                trail=[url],
                status=200,
                headers={"content-type": "text/html"},
                body=self._page.encode(),
            )
        return build_result(trail=[url], status=404)


def _fetch_page(page: str) -> _CountingFetch:
    return _CountingFetch(page)


class _RecordingSink:
    enabled = True

    def __init__(self):
        self.copied = 0

    def copy_website(self, snapshot) -> None:
        self.copied += 1


class _FailingSink(_RecordingSink):
    def copy_website(self, snapshot) -> None:
        raise ConnectionError("postgres is down")


def _wait_until(condition, seconds: float = 10) -> None:
    deadline = time.monotonic() + seconds
    while not condition() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert condition()


@pytest.fixture
def store(tmp_path):
    scrape_store = ScrapeStore(str(tmp_path))
    yield scrape_store
    scrape_store.close()


@pytest.fixture
def counting_fetch():
    return _fetch_page(PAGE)


@pytest.fixture
def failing_sink():
    return _FailingSink()


@pytest.fixture
def recording_sink():
    return _RecordingSink()


def _scrape_with_two_leads(store: ScrapeStore, websites=None) -> str:
    websites = websites or ["https://bakery.studio.de"]
    scrape_id = store.create_scrape("nightly", _settings())
    store.append_rows(
        scrape_id,
        [_lead("Bakery", website=websites[0]), _lead("No site")]
        + [_lead(f"Branch {n}", website=site) for n, site in enumerate(websites[1:])],
    )
    return scrape_id


def _finished_website_ids_for_test(store: ScrapeStore) -> list[str]:
    return CrawlBuffer(CrawlStore(store)).finished_website_ids()


def test_one_failing_website_does_not_stop_another(store, monkeypatch):
    original_run = WebsiteCrawl.run

    def run(self):
        if "bad.de" in self._website:
            raise RuntimeError("boom")
        return original_run(self)

    monkeypatch.setattr(WebsiteCrawl, "run", run)
    scrape_id = _scrape_with_two_leads(
        store, websites=["https://bad.de", "https://good.de"]
    )
    CrawlStore(store).queue_websites(scrape_id)
    worker = CrawlerWorker(
        CrawlStore(store), fetch=_fetch_page(PAGE), sink=PostgresSink(config=None)
    )
    worker.start()
    _wait_until(lambda: CrawlStore(store).count_pending_websites(scrape_id) == 0)
    worker.shutdown(wait=2)

    rows = {r["website"]: r for r in store.read_rows(scrape_id)}
    assert rows["https://good.de"]["emails"] == "hi@bakery.studio.de"
    buffer = CrawlBuffer(CrawlStore(store))
    websites = [buffer.read_website(i).website for i in buffer.finished_website_ids()]
    statuses = {w["origin"]: (w["status"], w["error_code"]) for w in websites}
    assert statuses["https://bad.de"] == ("failed", "crawl_error")


def test_website_whose_run_never_starts_is_marked_failed(store, monkeypatch):
    def create_run(self, **kwargs):
        raise RuntimeError("no run")

    monkeypatch.setattr(CrawlBuffer, "create_run", create_run)
    scrape_id = _scrape_with_two_leads(store)
    CrawlStore(store).queue_websites(scrape_id)
    sink = _RecordingSink()
    sink.enabled = False
    worker = CrawlerWorker(CrawlStore(store), fetch=_fetch_page(PAGE), sink=sink)  # type: ignore[arg-type]
    worker.start()
    _wait_until(lambda: CrawlStore(store).count_pending_websites(scrape_id) == 0)
    worker.shutdown(wait=2)

    buffer = CrawlBuffer(CrawlStore(store))
    (website_id,) = buffer.finished_website_ids()
    website = buffer.read_website(website_id).website
    assert website["status"] == "failed"
    assert website["error_code"] == "run_not_started"
    assert sink.copied == 0


def test_same_website_on_two_leads_is_crawled_once(store, counting_fetch):
    scrape_id = _scrape_with_two_leads(
        store, websites=["https://bakery.studio.de", "https://bakery.studio.de/shop"]
    )
    CrawlStore(store).queue_websites(scrape_id)
    worker = CrawlerWorker(
        CrawlStore(store), fetch=counting_fetch, sink=PostgresSink(config=None)
    )
    worker.start()
    _wait_until(lambda: CrawlStore(store).count_pending_websites(scrape_id) == 0)
    worker.shutdown(wait=2)

    assert counting_fetch.homepage_calls == 1
    rows = {r["website"]: r for r in store.read_rows(scrape_id)}
    assert rows["https://bakery.studio.de/shop"]["emails"] == "hi@bakery.studio.de"


def test_failed_copy_keeps_rows_and_next_start_resyncs(
    store, failing_sink, recording_sink
):
    scrape_id = _scrape_with_two_leads(store)
    CrawlStore(store).queue_websites(scrape_id)
    first = CrawlerWorker(CrawlStore(store), fetch=_fetch_page(PAGE), sink=failing_sink)
    first.start()
    _wait_until(lambda: CrawlStore(store).count_pending_websites(scrape_id) == 0)
    first.shutdown(wait=2)
    assert _finished_website_ids_for_test(store) != []

    second = CrawlerWorker(
        CrawlStore(store), fetch=_fetch_page(PAGE), sink=recording_sink
    )
    second.start()
    _wait_until(lambda: recording_sink.copied >= 1)
    second.shutdown(wait=2)
    assert _finished_website_ids_for_test(store) == []
