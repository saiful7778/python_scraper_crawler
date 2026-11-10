import pytest
from tortoise import Tortoise

from app.services.google_map_scraper.scrape.scrape_models import SCHEMA_VERSION
from app.services.google_map_scraper.scrape.scrape_store import ScrapeStore


@pytest.fixture
def store(tmp_path):
    scrape_store = ScrapeStore(str(tmp_path))
    yield scrape_store
    scrape_store.close()


async def _table_names() -> set[str]:
    rows = await Tortoise.get_connection("default").execute_query_dict(
        "SELECT name FROM sqlite_master WHERE type='table'"
    )
    return {row["name"] for row in rows}


def test_crawl_tables_exist_in_the_scrape_database(store):
    names = store._call(_table_names())
    for table in (
        "crawl_websites",
        "crawl_runs",
        "crawl_pages",
        "crawl_links",
        "crawl_raw_html",
        "crawl_findings",
        "crawl_events",
    ):
        assert table in names


def test_schema_version_is_bumped(store):
    assert SCHEMA_VERSION == 12
