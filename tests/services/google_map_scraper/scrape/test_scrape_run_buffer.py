import contextlib
from types import SimpleNamespace
from typing import cast

import fakeredis
import pytest
from redis.exceptions import ConnectionError as RedisConnectionError

from app.services.buffer.client import BufferError, BufferFullError
from app.services.buffer.known_keys import KnownKeys
from app.services.buffer.row_buffer import RowBuffer
from app.services.buffer.scrape_state import ScrapeState
from app.services.google_map_scraper.scrape.scrape_database import ScrapeDatabase
from app.services.google_map_scraper.scrape.scrape_run import ScrapeRun
from app.services.google_map_scraper.scrape.scrape_settings import ScrapeSettings
from app.services.google_map_scraper.scrape.scrape_types import LeadRow, ScrapeRecord

SCRAPE_ID = "s1"
TTL_SECONDS = 3600


@pytest.fixture
def fake_redis():
    return fakeredis.FakeRedis(decode_responses=True)


@pytest.fixture
def database(postgres):
    scrape_database = ScrapeDatabase(postgres)
    yield scrape_database
    scrape_database.close()


@pytest.fixture(autouse=True)
def offline_websites(monkeypatch):
    monkeypatch.setattr(
        "app.services.google_map_scraper.scrape.scrape_run.resolve_website",
        lambda raw: raw,
    )


def lead(title: str, website: str = "") -> LeadRow:
    return cast(
        LeadRow,
        {"title": title, "website": website, "link": f"https://maps.example/{title}"},
    )


def fake_scrape_run(database, redis, known_keys=frozenset(), **overrides) -> ScrapeRun:
    settings = ScrapeSettings(keywords=("bakery",), **overrides)
    settings_data = settings.model_dump(by_alias=True, mode="json")
    ScrapeState(redis, TTL_SECONDS).create(SCRAPE_ID, settings_data)
    KnownKeys(redis, TTL_SECONDS).load(SCRAPE_ID, known_keys)
    scrape = cast(
        ScrapeRecord,
        {
            "ID": SCRAPE_ID,
            "Name": "bakery",
            "Date": "2026-11-11T00:00:00",
            "Status": "working",
            "Data": settings_data,
            "Error": "",
            "Progress": {},
        },
    )
    return ScrapeRun(database, scrape, True, redis, known_keys)


def test_lead_with_website_is_buffered(fake_redis, database):
    run = fake_scrape_run(database, fake_redis)
    run.accept([lead("Cafe", "https://cafe.example")])
    assert RowBuffer(fake_redis, TTL_SECONDS).length(SCRAPE_ID) == 1


def test_duplicate_lead_is_neither_buffered_nor_queued(fake_redis, database):
    run = fake_scrape_run(database, fake_redis)
    run.accept([lead("Cafe", "https://cafe.example")])
    accepted = run.accept([lead("Cafe", "https://cafe.example")])
    assert accepted == []
    assert RowBuffer(fake_redis, TTL_SECONDS).length(SCRAPE_ID) == 1


def test_lead_without_website_is_buffered(fake_redis, database):
    run = fake_scrape_run(database, fake_redis)
    run.accept([lead("Cafe", "")])
    assert RowBuffer(fake_redis, TTL_SECONDS).length(SCRAPE_ID) == 1


def test_lead_without_website_is_not_buffered_when_website_filter_is_on(
    fake_redis, database
):
    run = fake_scrape_run(database, fake_redis, has_website=True)
    accepted = run.accept([lead("Cafe", "")])
    assert accepted == []
    assert RowBuffer(fake_redis, TTL_SECONDS).length(SCRAPE_ID) == 0


def test_lead_with_website_is_buffered_when_website_filter_is_on(fake_redis, database):
    run = fake_scrape_run(database, fake_redis, has_website=True)
    run.accept([lead("Cafe", "https://cafe.example")])
    assert RowBuffer(fake_redis, TTL_SECONDS).length(SCRAPE_ID) == 1


def test_lead_without_phone_is_not_buffered_when_phone_filter_is_on(
    fake_redis, database
):
    run = fake_scrape_run(database, fake_redis, has_phone=True)
    accepted = run.accept([lead("Cafe", "https://cafe.example")])
    assert accepted == []
    assert RowBuffer(fake_redis, TTL_SECONDS).length(SCRAPE_ID) == 0


def test_known_key_lead_is_skipped(fake_redis, database):
    run = fake_scrape_run(database, fake_redis, known_keys=frozenset({"title:Cafe"}))
    accepted = run.accept([lead("Cafe", "https://cafe.example")])
    assert accepted == []
    assert RowBuffer(fake_redis, TTL_SECONDS).length(SCRAPE_ID) == 0


def test_failed_buffer_append_releases_the_lead_so_it_can_be_retried(
    fake_redis, database, monkeypatch
):
    run = fake_scrape_run(database, fake_redis)
    real_append = run._rows.append
    failures = []

    def fail_once(scrape_id, rows):
        if not failures:
            failures.append(scrape_id)
            raise BufferFullError("Redis memory limit reached")
        real_append(scrape_id, rows)

    monkeypatch.setattr(run._rows, "append", fail_once)
    with pytest.raises(BufferFullError):
        run.accept([lead("Cafe", "https://cafe.example")])
    accepted = run.accept([lead("Cafe", "https://cafe.example")])
    assert [row["title"] for row in accepted] == ["Cafe"]
    assert RowBuffer(fake_redis, TTL_SECONDS).length(SCRAPE_ID) == 1


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
        yield SimpleNamespace(tab=object(), refused=False)


def test_redis_error_while_buffering_stops_the_scrape(fake_redis, database, monkeypatch):
    settings = ScrapeSettings(keywords=("bakery", "cafe"))
    scrape_id = database.create("bakery", settings.model_dump(by_alias=True, mode="json"))
    run = ScrapeRun(database, database.get(scrape_id), True, fake_redis)
    collected = []

    def collect(tab, url, **kwargs):
        collected.append(url)
        kwargs["on_batch"]([lead("Cafe", "https://cafe.example")], 1)
        return [], "done", {"batches": 1, "dom_links": 0}

    def unreachable(scrape_id, rows):
        raise RedisConnectionError("Connection refused")

    monkeypatch.setattr(
        "app.services.google_map_scraper.scrape.scrape_run.BrowserSession",
        _FakeBrowserSession,
    )
    monkeypatch.setattr(
        "app.services.google_map_scraper.scrape.keyword_search.collect_leads",
        collect,
    )
    monkeypatch.setattr(run._rows, "append", unreachable)

    with pytest.raises(BufferError, match="Redis"):
        run.execute()
    assert len(collected) == 1
    assert run._stop.is_set()
