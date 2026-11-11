import json
import threading
import time

import fakeredis
import pytest
from redis.exceptions import ConnectionError as RedisConnectionError

from app.helpers.http_fetcher.models import build_result
from app.models import CrawlWebsiteModel
from app.services.buffer.client import BufferError
from app.services.buffer.row_buffer import RowBuffer
from app.services.buffer.scrape_state import ScrapeState
from app.services.buffer.website_queue import WebsiteQueue
from app.services.google_map_scraper import scrape_runner
from app.services.google_map_scraper.scrape.engine_config import EngineConfig
from app.services.google_map_scraper.scrape.scrape_database import ScrapeDatabase
from app.services.google_map_scraper.scrape.scrape_run import _latest_copies
from app.services.google_map_scraper.scrape_runner import (
    ScrapeDeletedError,
    ScrapeFlushError,
    ScrapeInFlightError,
    run_scrape,
)
from app.services.google_map_scraper.scrape.scrape_settings import ScrapeSettings
from app.services.google_map_scraper.scrape.scrape_status import (
    ScrapeStage,
    ScrapeStatus,
)
from tests.services.google_map_scraper.scrape.conftest import fake_collect, in_database


class _FirstFetchIsSlow:
    """Holds the crawler thread on its first website, so the time limit runs out first."""

    def __init__(self):
        self.calls = 0

    def fetch_result(self, url, *, timeout=None, **kwargs):
        self.calls += 1
        if self.calls == 1:
            time.sleep(1.2)
        return build_result(trail=[url], status=404)


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


@pytest.fixture
def one_crawler(monkeypatch):
    monkeypatch.setattr(
        "app.services.google_map_scraper.scrape.scrape_runner.default_worker_count",
        lambda: 1,
    )


def _lead(title: str, website: str) -> dict:
    return {"title": title, "website": website, "latitude": 52.5, "longitude": 13.4}


PAGE = '<a href="mailto:hi@bakery-test.de">mail</a>'


class _BakeryFetcher:
    def fetch_result(self, url, *, timeout=None, **kwargs):
        if url.rstrip("/") == "https://bakery-test.de":
            return build_result(
                trail=[url],
                status=200,
                headers={"content-type": "text/html"},
                body=PAGE.encode(),
            )
        return build_result(trail=[url], status=404)


SOCIAL_HOME = '<a href="/contact">c</a><a href="https://instagram.com/plain">ig</a>'
SOCIAL_CONTACT = '<a href="https://instagram.com/two">ig</a>'


class _TwoInstagramsFetcher:
    def fetch_result(self, url, *, timeout=None, **kwargs):
        pages = {
            "https://bakery-test.de": SOCIAL_HOME,
            "https://bakery-test.de/contact": SOCIAL_CONTACT,
        }
        body = pages.get(url.rstrip("/"))
        if body is None:
            return build_result(trail=[url], status=404)
        return build_result(
            trail=[url],
            status=200,
            headers={"content-type": "text/html"},
            body=body.encode(),
        )


async def _count_crawl_websites() -> int:
    return await CrawlWebsiteModel.all().count()


def test_run_scrape_returns_rows_with_the_emails_their_crawl_found(
    config, postgres, fake_maps, redis_client, monkeypatch
):
    monkeypatch.setattr(
        "app.services.google_map_scraper.scrape.scrape_runner.WebPageFetcher",
        _BakeryFetcher,
    )
    result = run_scrape(
        "nightly",
        ScrapeSettings(keywords=("bakery",), email=True),
        config=config,
        redis=redis_client,
        postgres=postgres,
    )
    assert result.status == ScrapeStatus.OK
    assert [row["title"] for row in result.rows] == ["Bakery Test"]
    assert result.rows[0]["emails"] == "hi@bakery-test.de"
    assert WebsiteQueue(redis_client, 3600).scrape_ids_with_work() == []
    assert in_database(postgres, _count_crawl_websites) == 1
    assert redis_client.keys("scrape:*") == []


def test_socials_of_one_platform_are_joined_into_the_row(
    config, postgres, fake_maps, redis_client, monkeypatch
):
    monkeypatch.setattr(
        "app.services.google_map_scraper.scrape.scrape_runner.WebPageFetcher",
        _TwoInstagramsFetcher,
    )
    result = run_scrape(
        "nightly",
        ScrapeSettings(keywords=("bakery",), socials=True),
        config=config,
        redis=redis_client,
        postgres=postgres,
    )
    assert result.rows[0]["instagram"] == (
        "https://instagram.com/plain, https://instagram.com/two"
    )


def test_run_scrape_skips_leads_whose_key_is_already_known(
    config, postgres, fake_maps, redis_client, monkeypatch
):
    monkeypatch.setattr(
        "app.services.google_map_scraper.scrape.scrape_runner.WebPageFetcher",
        _BakeryFetcher,
    )
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


def test_crawl_postgres_refuses_stays_in_redis_and_stops_the_scrape(
    config, postgres, fake_maps, redis_client, monkeypatch
):
    monkeypatch.setattr(
        "app.services.google_map_scraper.scrape.scrape_runner.WebPageFetcher",
        _NoSite,
    )

    async def refuse(snapshot):
        raise RuntimeError("the crawl table is locked")

    monkeypatch.setattr(
        "app.services.google_map_scraper.scrape.scrape_runner.store_crawl", refuse
    )
    with pytest.raises(ScrapeFlushError, match="crawled website"):
        run_scrape(
            "cafe @ Austin",
            ScrapeSettings(keywords=("bakery",)),
            config=config,
            redis=redis_client,
            postgres=postgres,
        )
    assert redis_client.keys("scrape:*") != []


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


def test_website_the_time_limit_leaves_unchecked_is_stored_as_skipped(
    config,
    postgres,
    fake_maps,
    redis_client,
    one_crawler,
    monkeypatch,
):
    monkeypatch.setattr(
        "app.services.google_map_scraper.scrape.keyword_search.collect_leads",
        fake_collect(
            _lead("Cafe A", "https://a-cafe.de"), _lead("Cafe B", "https://b-cafe.de")
        ),
    )
    monkeypatch.setattr(
        "app.services.google_map_scraper.scrape.scrape_runner.WebPageFetcher",
        _FirstFetchIsSlow,
    )
    result = run_scrape(
        "cafes",
        ScrapeSettings(keywords=("cafe",), max_time=1),
        config=config,
        redis=redis_client,
        postgres=postgres,
    )
    assert len(result.rows) == 2
    statuses = in_database(postgres, _statuses_by_host)
    assert set(statuses) == {"a-cafe.de", "b-cafe.de"}
    assert statuses["b-cafe.de"] == "skipped"
    assert redis_client.keys("scrape:*") == []


def _join_crawlers() -> None:
    for thread in threading.enumerate():
        if thread.name.startswith("crawler-"):
            thread.join(5)


def _crawl_statuses(redis_client) -> dict[str, str]:
    documents = [
        json.loads(redis_client.get(key))["website"]
        for key in redis_client.keys("scrape:*:crawl:*")
    ]
    return {document["host"]: document["status"] for document in documents}


def test_website_still_being_checked_when_the_pool_stops_keeps_the_scrape_resumable(
    config, postgres, fake_maps, redis_client, one_crawler, monkeypatch
):
    release = threading.Event()

    class _Blocked:
        def fetch_result(self, url, *, timeout=None, **kwargs):
            release.wait(10)
            return build_result(trail=[url], status=404)

    monkeypatch.setattr(
        "app.services.google_map_scraper.scrape.scrape_runner.WebPageFetcher",
        _Blocked,
    )
    monkeypatch.setattr(scrape_runner, "_POOL_STOP_SECONDS", 0.2)
    try:
        with pytest.raises(ScrapeInFlightError, match="bakery-test.de"):
            run_scrape(
                "cafe @ Austin",
                ScrapeSettings(keywords=("bakery",), max_time=2),
                config=config,
                redis=redis_client,
                postgres=postgres,
            )
    finally:
        release.set()
        _join_crawlers()
    assert "skipped" not in _crawl_statuses(redis_client).values()
    assert redis_client.keys("scrape:*") != []


def test_queued_website_is_not_skipped_when_a_website_is_still_in_flight(
    config, postgres, fake_maps, redis_client, one_crawler, monkeypatch
):
    release = threading.Event()
    calls = []

    class _FirstBlocks:
        def fetch_result(self, url, *, timeout=None, **kwargs):
            calls.append(url)
            if len(calls) == 1:
                release.wait(10)
            return build_result(trail=[url], status=404)

    monkeypatch.setattr(
        "app.services.google_map_scraper.scrape.scrape_runner.WebPageFetcher",
        _FirstBlocks,
    )
    monkeypatch.setattr(
        "app.services.google_map_scraper.scrape.keyword_search.collect_leads",
        fake_collect(
            _lead("Cafe A", "https://a-cafe.de"), _lead("Cafe B", "https://b-cafe.de")
        ),
    )
    monkeypatch.setattr(scrape_runner, "_POOL_STOP_SECONDS", 0.2)
    try:
        with pytest.raises(ScrapeInFlightError, match="a-cafe.de"):
            run_scrape(
                "cafes",
                ScrapeSettings(keywords=("cafe",), max_time=2),
                config=config,
                redis=redis_client,
                postgres=postgres,
            )
    finally:
        release.set()
        _join_crawlers()
    assert "b-cafe.de" not in _crawl_statuses(redis_client)
    assert "b-cafe.de" not in in_database(postgres, _statuses_by_host)
    assert redis_client.keys("scrape:*") != []


async def _statuses_by_host() -> dict[str, str]:
    return {row.host: row.status for row in await CrawlWebsiteModel.all()}


class _NoSite:
    def fetch_result(self, url, *, timeout=None, **kwargs):
        return build_result(trail=[url], status=404)


def test_resumed_scrape_continues_from_redis_and_is_finished_with_its_outcome(
    config, postgres, fake_maps, redis_client, database, monkeypatch
):
    monkeypatch.setattr(
        "app.services.google_map_scraper.scrape.scrape_runner.WebPageFetcher",
        _NoSite,
    )
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
