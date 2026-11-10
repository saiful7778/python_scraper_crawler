import os
import socket
from typing import cast

import pytest
from tortoise import Tortoise

from app.models import (
    CrawlEventModel,
    CrawlFindingModel,
    CrawlLinkModel,
    CrawlPageModel,
    CrawlRawHTMLModel,
    CrawlRunModel,
    CrawlWebsiteModel,
)
from app.services.crawler.findings import Finding
from app.services.crawler.storage.buffer import (
    CrawlBuffer,
    LinkSeed,
    PageResult,
    PageSeed,
)
from app.services.crawler.storage.crawl_store import CrawlStore
from app.services.crawler.storage.postgres_sink import PostgresSink, event_id
from app.services.google_map_scraper.scrape.scrape_models import ScrapeLeadModel
from app.services.google_map_scraper.scrape.scrape_settings import ScrapeSettings
from app.services.google_map_scraper.scrape.scrape_store import ScrapeStore
from app.services.google_map_scraper.scrape.scrape_types import LeadRow, SettingsData
from config.tortoise import TortoiseConfig

HOME = "https://a.de/"
ABOUT = "https://a.de/about"
ALL_MODELS = (
    CrawlWebsiteModel,
    CrawlRunModel,
    CrawlPageModel,
    CrawlLinkModel,
    CrawlRawHTMLModel,
    CrawlFindingModel,
    CrawlEventModel,
)


def _lead() -> LeadRow:
    row: dict[str, object] = {key: "" for key in LeadRow.__required_keys__}
    row["latitude"] = None
    row["longitude"] = None
    row["title"] = "Bakery"
    row["website"] = "https://a.de"
    return row  # type: ignore[return-value]


async def _first_lead_id(scrape_id: str) -> int:
    lead = await ScrapeLeadModel.filter(scrape_id=scrape_id).first()
    assert lead is not None
    return lead.id


@pytest.fixture
def snapshot(tmp_path):
    store = ScrapeStore(str(tmp_path))
    buffer = CrawlBuffer(CrawlStore(store))
    settings = ScrapeSettings(keywords=("bakery",))
    scrape_id = store.create_scrape(
        "t", cast(SettingsData, settings.model_dump(by_alias=True, mode="json"))
    )
    store.append_rows(scrape_id, [_lead()])
    website_id = buffer.create_website(
        scrape_id=scrape_id,
        lead_position=0,
        scrape_lead_id=store.run_async(_first_lead_id(scrape_id)),
        host="a.de",
        registrable_domain="a.de",
        origin="https://a.de",
    )
    run_id = buffer.create_run(
        website_id=website_id, requested_url=HOME, config={"max_pages": 5}
    )
    pages = buffer.add_pages(
        run_id,
        [
            PageSeed(HOME, HOME, 0, "seed"),
            PageSeed(ABOUT, ABOUT, 1, "link"),
        ],
    )
    buffer.save_page(
        pages[HOME],
        PageResult(status="fetched", http_status=200, html="<html>home</html>"),
    )
    buffer.save_page(pages[ABOUT], PageResult(status="fetched", http_status=200))
    buffer.add_links(pages[HOME], [LinkSeed(ABOUT, "About", None, True)])
    buffer.add_findings(
        website_id=website_id,
        run_id=run_id,
        page_id=pages[HOME],
        findings=[Finding(group="email", key="info", value="info@a.de", extra=None)],
    )
    buffer.add_event(run_id=run_id, step="crawl", level="info", message="started")
    buffer.finish_run(
        run_id,
        status="done",
        progress={"pages": 2},
        error_code=None,
        error_message=None,
    )
    yield buffer.read_website(website_id)
    store.close()


def _sqlite_config(path) -> TortoiseConfig:
    return TortoiseConfig(
        connections={
            "default": {
                "engine": "tortoise.backends.sqlite",
                "credentials": {"file_path": str(path)},
            }
        }
    )


async def _create_tables() -> None:
    await Tortoise.generate_schemas(safe=True)


async def _count(model, **filters) -> int:
    return await model.filter(**filters).count()


async def _delete_website(website_id) -> None:
    await CrawlWebsiteModel.filter(id=website_id).delete()


def _prepare(sink: PostgresSink) -> PostgresSink:
    sink.run_async(_create_tables())
    return sink


def _all_counts(sink: PostgresSink) -> dict[str, int]:
    return {model.__name__: sink.run_async(_count(model)) for model in ALL_MODELS}


def _test_database_config() -> TortoiseConfig | None:
    database = os.getenv("TEST_DB_DATABASE")
    if not database:
        return None
    config = TortoiseConfig()
    credentials = config.connections["default"]["credentials"]
    if database == credentials["database"]:
        return None
    credentials["database"] = database
    try:
        with socket.create_connection(
            (credentials["host"], credentials["port"]), timeout=1
        ):
            return config
    except OSError:
        return None


@pytest.fixture
def pg_sink(snapshot):
    config = _test_database_config()
    if config is None:
        pytest.skip(
            "set TEST_DB_DATABASE to a reachable Postgres test database "
            "that differs from DB_DATABASE"
        )
    sink = _prepare(PostgresSink(config))
    yield sink
    sink.run_async(_delete_website(snapshot.website["id"]))
    sink.close()


@pytest.fixture
def sqlite_sink(tmp_path):
    sink = _prepare(PostgresSink(_sqlite_config(tmp_path / "copy.db")))
    yield sink
    sink.close()


@pytest.fixture
def unreachable_sink():
    config = TortoiseConfig()
    config.connections["default"]["credentials"].update(
        host="127.0.0.1", port=1, database="no_such_database_for_tests"
    )
    sink = PostgresSink(config)
    yield sink
    sink.close()


def test_disabled_sink_ignores_copies(snapshot):
    sink = PostgresSink(config=None)
    assert sink.enabled is False
    sink.copy_website(snapshot)
    sink.close()


def test_from_environment_is_disabled_without_db_host(monkeypatch):
    monkeypatch.delenv("DB_HOST", raising=False)
    assert PostgresSink.from_environment().enabled is False


def test_from_environment_is_enabled_with_db_host(monkeypatch):
    monkeypatch.setenv("DB_HOST", "db.example")
    sink = PostgresSink.from_environment()
    try:
        assert sink.enabled is True
    finally:
        sink.close()


def test_copy_writes_every_table(sqlite_sink, snapshot):
    sqlite_sink.copy_website(snapshot)

    counts = _all_counts(sqlite_sink)
    assert counts["CrawlWebsiteModel"] == 1
    assert counts["CrawlRunModel"] == 1
    assert counts["CrawlPageModel"] == len(snapshot.pages)
    assert counts["CrawlLinkModel"] == len(snapshot.links) == 1
    assert counts["CrawlRawHTMLModel"] == len(snapshot.raw_html) == 1
    assert counts["CrawlFindingModel"] == len(snapshot.findings) == 1
    assert counts["CrawlEventModel"] == len(snapshot.events) == 1


def test_copy_twice_gives_the_same_rows_without_postgres(sqlite_sink, snapshot):
    sqlite_sink.copy_website(snapshot)
    first = _all_counts(sqlite_sink)
    sqlite_sink.copy_website(snapshot)
    assert _all_counts(sqlite_sink) == first


def test_copy_updates_a_changed_row(sqlite_sink, snapshot):
    sqlite_sink.copy_website(snapshot)
    snapshot.website = {**snapshot.website, "status": "failed"}
    sqlite_sink.copy_website(snapshot)

    async def status():
        return (await CrawlWebsiteModel.get(id=snapshot.website["id"])).status

    assert sqlite_sink.run_async(status()) == "failed"


@pytest.mark.postgres
def test_copy_twice_gives_the_same_rows(pg_sink, snapshot):
    pg_sink.copy_website(snapshot)
    first = _all_counts(pg_sink)
    pg_sink.copy_website(snapshot)
    assert _all_counts(pg_sink) == first
    assert first["CrawlPageModel"] == len(snapshot.pages)
    assert first["CrawlEventModel"] == len(snapshot.events)


def test_event_ids_are_stable_per_run_and_differ_across_runs():
    assert event_id("run-a", 1) == event_id("run-a", 1)
    assert event_id("run-a", 1) != event_id("run-b", 1)
    assert event_id("run-a", 1) != event_id("run-a", 2)


def test_copy_maps_event_ids_from_the_run_and_the_sqlite_id(sqlite_sink, snapshot):
    sqlite_sink.copy_website(snapshot)
    run_id = str(snapshot.runs[0]["id"])
    sqlite_id = snapshot.events[0]["id"]

    async def copied_ids():
        return [
            str(event.id)
            for event in await CrawlEventModel.filter(run_id=run_id).order_by("id")
        ]

    assert sqlite_sink.run_async(copied_ids()) == [str(event_id(run_id, sqlite_id))]


def test_database_guard_refuses_the_dev_database(monkeypatch):
    monkeypatch.setenv("DB_DATABASE", "dev_database")
    monkeypatch.setenv("TEST_DB_DATABASE", "dev_database")
    assert _test_database_config() is None


def test_copy_failure_raises(unreachable_sink, snapshot):
    with pytest.raises(OSError):
        unreachable_sink.copy_website(snapshot)
