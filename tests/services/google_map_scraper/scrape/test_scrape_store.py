import pytest

from app.services.crawler.storage.crawl_store import CrawlStore
from app.services.google_map_scraper.scrape.scrape_store import ScrapeStore
from app.services.google_map_scraper.scrape.scrape_types import LeadRow, SettingsData


def _settings(**overrides) -> SettingsData:
    settings: dict = {
        "keywords": ["bakery", "cafe"],
        "lang": "de",
        "country": "de",
        "lat": 52.5,
        "lon": 13.4,
        "zoom": 15,
        "depth": 2,
        "max_time": 600,
        "fast_mode": True,
        "email": True,
        "socials": False,
        "proxies": ["http://proxy-a:8080", "http://proxy-b:8080"],
        "limit": 50,
        "crawl_max_pages": 100,
        "crawl_max_depth": 3,
        "crawl_store_html": True,
    }
    settings.update(overrides)
    return settings  # type: ignore[return-value]


def _lead(title: str, **extra: str) -> LeadRow:
    row: dict[str, object] = {key: "" for key in LeadRow.__required_keys__}
    row["latitude"] = None
    row["longitude"] = None
    row["title"] = title
    row.update(extra)
    return row  # type: ignore[return-value]


@pytest.fixture
def store(tmp_path):
    scrape_store = ScrapeStore(str(tmp_path))
    yield scrape_store
    scrape_store.close()


def test_settings_round_trip(store):
    settings = _settings()
    scrape_id = store.create_scrape("nightly", settings)
    assert store.get_scrape(scrape_id)["Data"] == settings


def test_unset_optional_settings_come_back_as_stored(store):
    settings = _settings(lat=None, lon=None, limit=None, proxies=[], keywords=["bar"])
    scrape_id = store.create_scrape("plain", settings)
    assert store.get_scrape(scrape_id)["Data"] == settings


def test_keyword_and_proxy_order_is_kept(store):
    settings = _settings(keywords=["c", "a", "b"], proxies=["http://z", "http://y"])
    scrape_id = store.create_scrape("ordered", settings)
    data = store.get_scrape(scrape_id)["Data"]
    assert data["keywords"] == ["c", "a", "b"]
    assert data["proxies"] == ["http://z", "http://y"]


def test_list_scrapes_includes_settings(store):
    store.create_scrape("first", _settings(keywords=["one"]))
    store.create_scrape("second", _settings(keywords=["two"]))
    names = {
        scrape["Name"]: scrape["Data"]["keywords"] for scrape in store.list_scrapes()
    }
    assert names == {"first": ["one"], "second": ["two"]}


def test_leads_round_trip(store):
    scrape_id = store.create_scrape("leads", _settings())
    plain = _lead("Plain")
    store.append_rows(scrape_id, [plain])
    assert store.read_rows(scrape_id) == [plain]


def test_website_fields_are_read_back_with_the_lead(store):
    scrape_id = store.create_scrape("socials", _settings())
    store.append_rows(scrape_id, [_lead("Socials", website="https://bakery.studio.de")])
    CrawlStore(store).queue_websites(scrape_id)
    CrawlStore(store).save_website_result(
        scrape_id,
        0,
        {"emails": "a@b.de"},
        [("instagram", "@plain"), ("instagram", "@two")],
    )
    (row,) = store.read_rows(scrape_id)
    assert row["instagram"] == "@plain, @two"
    assert row["emails"] == "a@b.de"
    assert "facebook" not in row


def test_review_start_counts_are_stored_per_row(store):
    scrape_id = store.create_scrape("reviews", _settings())
    row = _lead("Reviewed", review_star_1_count="17", review_star_5_count="13")
    store.append_rows(scrape_id, [row])
    assert store.read_rows(scrape_id) == [row]


def test_replace_and_truncate_rows(store):
    scrape_id = store.create_scrape("rows", _settings())
    store.append_rows(scrape_id, [_lead("a"), _lead("b"), _lead("c")])
    store.truncate_rows(scrape_id, 2)
    assert [row["title"] for row in store.read_rows(scrape_id)] == ["a", "b"]
    store.replace_rows(scrape_id, [_lead("z")])
    assert [row["title"] for row in store.read_rows(scrape_id)] == ["z"]


def test_delete_scrape_removes_settings_and_leads(store):
    scrape_id = store.create_scrape("gone", _settings())
    store.append_rows(scrape_id, [_lead("x")])
    assert store.delete_scrape(scrape_id)
    assert store.get_scrape(scrape_id) is None
    assert store.read_rows(scrape_id) == []
