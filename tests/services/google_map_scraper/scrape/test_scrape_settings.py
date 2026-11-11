import pytest
from pydantic import ValidationError

from app.services.buffer.scrape_state import ScrapeState
from app.services.google_map_scraper.scrape.scrape_settings import ScrapeSettings


def test_crawl_options_have_defaults_and_bounds():
    settings = ScrapeSettings(keywords=("bakery",))
    assert settings.crawl_max_pages == 100
    assert settings.crawl_max_depth == 3
    with pytest.raises(ValidationError):
        ScrapeSettings(keywords=("bakery",), crawl_max_pages=0)
    with pytest.raises(ValidationError):
        ScrapeSettings(keywords=("bakery",), crawl_max_depth=11)


def test_crawl_options_round_trip_through_the_scrape_state(fake_redis):
    settings = ScrapeSettings(keywords=("bakery",), crawl_max_pages=7, crawl_max_depth=1)
    state = ScrapeState(fake_redis, 3600)
    state.create("s1", settings.model_dump(by_alias=True, mode="json"))
    data = state.get_settings("s1")
    assert data["crawl_max_pages"] == 7
    assert data["crawl_max_depth"] == 1
