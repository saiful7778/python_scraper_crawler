import pytest
from pydantic import ValidationError

from app.services.google_map_scraper.scrape.scrape_settings import ScrapeSettings
from app.services.google_map_scraper.scrape.scrape_store import ScrapeStore


@pytest.fixture
def store(tmp_path):
    scrape_store = ScrapeStore(str(tmp_path))
    yield scrape_store
    scrape_store.close()


def test_crawl_options_have_defaults_and_bounds():
    settings = ScrapeSettings(keywords=("bakery",))
    assert settings.crawl_max_pages == 100
    assert settings.crawl_max_depth == 3
    assert settings.crawl_store_html is True
    with pytest.raises(ValidationError):
        ScrapeSettings(keywords=("bakery",), crawl_max_pages=0)
    with pytest.raises(ValidationError):
        ScrapeSettings(keywords=("bakery",), crawl_max_depth=11)


def test_crawl_options_round_trip_through_the_scrape_store(store):
    settings = ScrapeSettings(keywords=("bakery",), crawl_max_pages=7, crawl_max_depth=1)
    scrape_id = store.create_scrape("t", settings.model_dump(by_alias=True, mode="json"))
    data = store.get_scrape(scrape_id)["Data"]
    assert data["crawl_max_pages"] == 7
    assert data["crawl_max_depth"] == 1
    assert data["crawl_store_html"] is True
