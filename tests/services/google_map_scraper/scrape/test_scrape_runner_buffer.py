import fakeredis
import pytest
from redis.exceptions import ConnectionError as RedisConnectionError

from app.services.buffer.client import BufferError
from app.services.buffer.row_buffer import RowBuffer
from app.services.buffer.scrape_state import ScrapeState
from app.services.google_map_scraper.scrape.engine_config import EngineConfig
from app.services.google_map_scraper.scrape.scrape_database import ScrapeDatabase
from app.services.google_map_scraper.scrape.scrape_run import _latest_copies
from app.services.google_map_scraper.scrape.scrape_settings import ScrapeSettings
from app.services.google_map_scraper.scrape.scrape_status import (
    ScrapeStage,
    ScrapeStatus,
)
from app.services.google_map_scraper.scraper import (
    ScrapeDeletedError,
    run_scrape,
)


@pytest.fixture
def redis_client():
    return fakeredis.FakeRedis(decode_responses=True)


@pytest.fixture
def database(postgres):
    scrape_database = ScrapeDatabase(postgres)
    yield scrape_database
    scrape_database.close()


@pytest.fixture
def config(tmp_path):
    return EngineConfig(data_dir=tmp_path, headless=True)


def _lead(title: str, website: str) -> dict:
    return {"title": title, "website": website, "latitude": 52.5, "longitude": 13.4}


def test_run_scrape_skips_leads_whose_key_is_already_known(
    config, postgres, fake_maps, redis_client
):
    result = run_scrape(
        "nightly",
        ScrapeSettings(keywords=("bakery",), email=True),
        config=config,
        known_keys=frozenset({"title:Bakery Test"}),
        redis=redis_client,
        postgres=postgres,
    )
    assert result.status == ScrapeStatus.OK
    assert result.rows == []


def test_scrape_result_rows_come_from_memory_and_redis_is_clean(
    config, postgres, fake_maps, redis_client
):
    result = run_scrape(
        "cafe @ Austin",
        ScrapeSettings(keywords=("bakery",)),
        config=config,
        known_keys=frozenset(),
        redis=redis_client,
        postgres=postgres,
    )
    assert [row["title"] for row in result.rows] == ["Bakery Test"]
    assert redis_client.keys("scrape:*") == []


def test_redis_error_during_the_run_stops_it_and_names_redis(
    config, postgres, fake_maps, redis_client, monkeypatch
):
    def unreachable(self, scrape_id, rows):
        raise RedisConnectionError("Connection refused")

    monkeypatch.setattr(RowBuffer, "append", unreachable)
    with pytest.raises(BufferError, match="Redis"):
        run_scrape(
            "cafe @ Austin",
            ScrapeSettings(keywords=("bakery", "cafe")),
            config=config,
            redis=redis_client,
            postgres=postgres,
        )


def test_scrape_deleted_while_it_runs_raises_scrape_deleted(
    config, postgres, fake_maps, redis_client, monkeypatch
):
    monkeypatch.setattr(ScrapeDatabase, "get", lambda self, scrape_id: None)
    with pytest.raises(ScrapeDeletedError):
        run_scrape(
            "cafe @ Austin",
            ScrapeSettings(keywords=("bakery",)),
            config=config,
            redis=redis_client,
            postgres=postgres,
        )


def test_resumed_scrape_continues_from_redis_and_is_finished_with_its_outcome(
    config, postgres, fake_maps, redis_client, database
):
    settings = ScrapeSettings(keywords=("cafe",))
    settings_data = settings.model_dump(by_alias=True, mode="json")
    interrupted = database.create("cafes", settings_data)
    database.set_status(interrupted, ScrapeStatus.WORKING)
    ScrapeState(redis_client, 3600).create(interrupted, settings_data)
    ScrapeState(redis_client, 3600).save_progress(
        interrupted, {"stage": ScrapeStage.WEBSITES, "done": 1, "leads": 1}
    )
    plain = {**_lead("Cafe A", "https://a-cafe.de"), "link": "https://maps/a"}
    reviewed = {**plain, "phone": "+49 30 1"}
    RowBuffer(redis_client, 3600).append(interrupted, [plain, reviewed])

    run_scrape(
        "new",
        settings,
        config=config,
        redis=redis_client,
        postgres=postgres,
    )

    record = database.get(interrupted)
    assert record["Status"] == ScrapeStatus.OK
    assert record["Progress"]["resumes"] == 1
    assert redis_client.keys("scrape:*") == []


def test_latest_copies_keeps_the_last_copy_of_each_lead_in_first_seen_order():
    first = {"title": "Cafe A", "link": "https://maps/a", "phone": ""}
    other = {"title": "Cafe B", "link": "https://maps/b", "phone": ""}
    reviewed = {**first, "phone": "+49 30 1"}

    copies = _latest_copies([first, other, reviewed])

    assert copies == [reviewed, other]
