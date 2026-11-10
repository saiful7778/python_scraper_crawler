import contextlib

import pytest

from app.helpers.http_fetcher.models import build_result
from app.services.crawler.storage.models import CrawlWebsiteModel
from app.services.google_map_scraper.scrape.engine_config import EngineConfig
from app.services.google_map_scraper.scrape.scrape_runner import run_scrape
from app.services.google_map_scraper.scrape.scrape_settings import ScrapeSettings
from app.services.google_map_scraper.scrape.scrape_status import ScrapeStatus
from app.services.google_map_scraper.scrape.scrape_store import ScrapeStore

PAGE = '<a href="mailto:hi@bakery-test.de">mail</a>'
WEBSITE = "https://bakery-test.de"


class _FakeProxyTab:
    tab = object()
    refused = False


class _FakeBrowserSession:
    def __init__(self, locale: str, headless: bool):
        self.browser = None

    @property
    def is_dead(self) -> bool:
        return self.browser is None

    def start(self) -> None:
        self.browser = object()

    def restart(self) -> None:
        self.start()

    def kill(self) -> None:
        self.browser = None

    def close(self) -> None:
        self.browser = None

    @contextlib.contextmanager
    def open_tab(self, proxy):
        yield _FakeProxyTab()


def _collect_leads(tab, url, **kwargs):
    lead = {
        "title": "Bakery Test",
        "website": WEBSITE,
        "latitude": 52.5,
        "longitude": 13.4,
    }
    return [lead], "done", {"batches": 1, "dom_links": 0}


class _FakeFetcher:
    def fetch_result(self, url, *, timeout=None, **kwargs):
        if url == WEBSITE + "/":
            return build_result(
                trail=[url],
                status=200,
                headers={"content-type": "text/html"},
                body=PAGE.encode(),
            )
        return build_result(trail=[url], status=404)


@pytest.fixture
def fake_maps(monkeypatch):
    monkeypatch.setattr(
        "app.services.google_map_scraper.scrape.scrape_run.BrowserSession",
        _FakeBrowserSession,
    )
    monkeypatch.setattr(
        "app.services.google_map_scraper.scrape.keyword_search.collect_leads",
        _collect_leads,
    )
    monkeypatch.setattr("app.services.crawler.worker.WebPageFetcher", _FakeFetcher)


def settings_with_email() -> ScrapeSettings:
    return ScrapeSettings(keywords=("bakery",), email=True)


def engine_config(tmp_path, fake_maps) -> EngineConfig:
    return EngineConfig(data_dir=tmp_path, headless=True)


async def _count_crawl_websites() -> int:
    return await CrawlWebsiteModel.all().count()


def crawl_rows_remaining(tmp_path) -> int:
    store = ScrapeStore(str(tmp_path))
    try:
        return store.run_async(_count_crawl_websites())
    finally:
        store.close()


def test_run_scrape_returns_rows_and_cleans_its_scrape_from_sqlite(
    tmp_path, monkeypatch, fake_maps
):
    monkeypatch.delenv("DB_HOST", raising=False)
    result = run_scrape(
        "nightly", settings_with_email(), config=engine_config(tmp_path, fake_maps)
    )
    assert result.status == ScrapeStatus.OK
    assert any(row["emails"] for row in result.rows)
    assert crawl_rows_remaining(tmp_path) == 0


def test_run_scrape_skips_leads_whose_key_is_already_known(
    tmp_path, monkeypatch, fake_maps
):
    monkeypatch.delenv("DB_HOST", raising=False)
    result = run_scrape(
        "nightly",
        settings_with_email(),
        config=engine_config(tmp_path, fake_maps),
        known_keys=frozenset({"title:Bakery Test"}),
    )
    assert result.status == ScrapeStatus.OK
    assert result.rows == []
