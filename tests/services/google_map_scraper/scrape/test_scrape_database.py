import pytest

from app.services.google_map_scraper.scrape.scrape_database import (
    ScrapeDatabase,
    ScrapeDatabaseError,
)
from app.services.google_map_scraper.scrape.scrape_status import ScrapeStatus
from tests.services.google_map_scraper.scrape.conftest import sqlite_config

SETTINGS = {"keywords": ["cafe"], "lang": "en", "max_time": 600}


@pytest.fixture
def database(postgres):
    scrape_database = ScrapeDatabase(postgres)
    yield scrape_database
    scrape_database.close()


def test_a_created_scrape_is_pending_and_can_be_claimed_once(database):
    scrape_id = database.create("cafe @ Austin", SETTINGS)

    assert database.get(scrape_id)["Status"] == ScrapeStatus.PENDING
    assert database.claim_next_pending()["ID"] == scrape_id
    assert database.get(scrape_id)["Status"] == ScrapeStatus.WORKING
    assert database.claim_next_pending() is None


def test_finish_stores_status_note_and_progress(database):
    scrape_id = database.create("cafe", SETTINGS)

    assert database.finish(scrape_id, ScrapeStatus.OK, "all good", {"leads": 3})

    record = database.get(scrape_id)
    assert record["Status"] == ScrapeStatus.OK
    assert record["Error"] == "all good"
    assert record["Progress"]["leads"] == 3


def test_a_deleted_scrape_reads_as_none_and_cannot_be_finished(database, postgres):
    assert database.get("00000000-0000-0000-0000-000000000000") is None
    assert not database.finish(
        "00000000-0000-0000-0000-000000000000", ScrapeStatus.OK, "", {}
    )


def test_interrupted_scrapes_are_resumed_up_to_the_limit(database):
    scrape_id = database.create("cafe", SETTINGS)
    database.claim_next_pending()

    assert database.requeue_interrupted(1) == (1, 0)
    assert database.pending_ids() == [scrape_id]

    database.claim_next_pending()
    assert database.requeue_interrupted(1) == (0, 1)
    assert database.get(scrape_id)["Status"] == ScrapeStatus.FAILED
    assert database.pending_ids() == []


def test_pending_ids_are_oldest_first(database):
    first = database.create("first", SETTINGS)
    second = database.create("second", SETTINGS)

    assert database.pending_ids() == [first, second]


def test_an_unreachable_database_raises_an_error_that_names_postgres(tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("")
    database = ScrapeDatabase(sqlite_config(blocker / "engine.db"))
    try:
        with pytest.raises(ScrapeDatabaseError, match="PostgreSQL"):
            database.get("00000000-0000-0000-0000-000000000000")
    finally:
        database.close()


def test_a_closed_database_refuses_calls(postgres):
    database = ScrapeDatabase(postgres)
    database.close()
    with pytest.raises(ScrapeDatabaseError, match="PostgreSQL"):
        database.pending_ids()
