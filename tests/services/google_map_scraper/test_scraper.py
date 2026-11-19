import queue
import threading
from collections.abc import Callable
from typing import cast

import fakeredis
import pytest

from app.services.buffer.flusher import Flusher
from app.services.buffer.row_buffer import RowBuffer
from app.services.buffer.scrape_state import ScrapeState
from app.services.google_map_scraper import scraper as scraper_module
from app.services.google_map_scraper.place_query import PlaceQuery
from app.services.google_map_scraper.scrape.engine_config import EngineConfig
from app.services.google_map_scraper.scrape.lead_filter import keep_lead
from app.services.google_map_scraper.scrape.scrape_database import ScrapeDatabase
from app.services.google_map_scraper.scrape.scrape_run import RunOutcome
from app.services.google_map_scraper.scrape.scrape_status import ScrapeStatus
from app.services.google_map_scraper.scrape.scrape_types import LeadRow, SettingsData
from app.services.google_map_scraper.scraper import (
    BoundingBoxError,
    PlaceScraper,
    UnsavedLeadsError,
    finalize_rows,
    parse_bounding_box,
)
from config.tortoise import TortoiseConfig
from tests.services.google_map_scraper.tile_fakes import Reply, TileEnv, done, row


@pytest.fixture
def redis():
    return fakeredis.FakeRedis(decode_responses=True)


def _query(**overrides) -> PlaceQuery:
    data = {"keywords": ["cafe"], "city": "Austin", "country": "us", **overrides}
    return PlaceQuery.model_validate(data)


def _row(**overrides) -> LeadRow:
    return cast(
        LeadRow,
        {
            "title": "Cafe",
            "latitude": 30.27,
            "longitude": -97.74,
            "phone": "",
            "website": "",
            **overrides,
        },
    )


def test_parse_bounding_box_returns_four_floats():
    assert parse_bounding_box("Austin", ["1", "2", "3", "4"]) == (1.0, 2.0, 3.0, 4.0)


def test_parse_bounding_box_raises_for_three_values():
    with pytest.raises(BoundingBoxError, match="has 3 values, expected 4"):
        parse_bounding_box("Austin", ["1", "2", "3"])


def test_keep_row_applies_the_phone_filter():
    query = _query(has_phone=True)
    assert keep_lead(_row(phone="+1 512"), query)
    assert not keep_lead(_row(phone=""), query)


def test_keep_row_applies_the_website_filter():
    query = _query(has_website=True)
    assert keep_lead(_row(website="https://cafe.example"), query)
    assert not keep_lead(_row(website=""), query)


@pytest.mark.parametrize("blank", ["   ", "\n\t"])
def test_keep_row_treats_whitespace_as_no_website_or_phone(blank):
    assert not keep_lead(_row(website=blank), _query(has_website=True))
    assert not keep_lead(_row(phone=blank), _query(has_phone=True))


def test_keep_row_keeps_everything_without_filters():
    assert keep_lead(_row(), _query())


def test_finalize_rows_drops_rows_outside_the_bounding_box():
    inside = _row(title="inside")
    outside = _row(title="outside", latitude=10.0, longitude=10.0)
    bounding_box = (30.0, 30.5, -98.0, -97.5)
    kept = finalize_rows([inside, outside], _query(), bounding_box)
    assert [row["title"] for row in kept] == ["inside"]


def test_finalize_rows_applies_the_limit():
    rows = [_row(title=str(index)) for index in range(5)]
    kept = finalize_rows(rows, _query(limit=2), None)
    assert [row["title"] for row in kept] == ["0", "1"]


def test_prepare_batch_runs_its_steps_in_order():
    from app.services.google_map_scraper.scraper import _prepare_batch

    calls: list[str] = []

    def first(rows):
        calls.append("first")

    def second(rows):
        calls.append("second")

    _prepare_batch(first, second)([])

    assert calls == ["first", "second"]


def test_final_flush_has_no_time_limit():
    from app.services.google_map_scraper import scraper

    assert not hasattr(scraper, "_FINAL_FLUSH_SECONDS")


def _grid_geocode(*a, **k):
    return ((1.0, 2.0), ["0", "1", "0", "1"])


def _geocode(place, with_bbox=False, country=None):
    return _grid_geocode() if with_bbox else (1.0, 2.0)


def _arrange(monkeypatch, behave, carried=()):
    env = TileEnv(behave, carried)
    monkeypatch.setattr(scraper_module, "ScrapeSession", env.factory())
    monkeypatch.setattr(scraper_module, "get_geocode", _geocode)
    return env


def test_grid_stops_when_a_tiles_leads_stay_unsaved(monkeypatch, redis):
    env = _arrange(monkeypatch, lambda kw, slot: Reply(unsaved=True))
    with pytest.raises(UnsavedLeadsError):
        PlaceScraper(redis).scrape(_query(grid=2))
    assert len(env.searched) == 1


class _RecordingDatabase:
    def __init__(self):
        self.finished: list[str] = []

    def finish(self, scrape_id, status, note, progress):
        self.finished.append(scrape_id)
        return True


def test_finish_saves_the_tiles_rows_before_it_checks_that_they_are_saved(redis):
    scrape_id = "tile-1"
    state = ScrapeState(redis, 3600)
    rows = RowBuffer(redis, 3600)
    state.create(
        scrape_id,
        cast(SettingsData, {"keywords": ["cafe"], "lang": "en", "max_time": 0}),
    )
    rows.append(scrape_id, [_row(title="A")])
    saved: list[tuple[str, str]] = []

    async def save(row, saved_id):
        saved.append((saved_id, row["title"]))

    flusher = Flusher(rows, state, save_row=save, crawl_sink=None)
    database = _RecordingDatabase()
    session = scraper_module.ScrapeSession.__new__(scraper_module.ScrapeSession)
    session._finished = {
        scrape_id: (
            RunOutcome(ScrapeStatus.OK, "", blocked=False, connection_lost=False),
            [_row(title="A")],
        )
    }
    session._state = state
    session._rows = rows
    session._database = cast(ScrapeDatabase, database)
    session._redis = redis
    session._flushers = [flusher]
    session._free_flushers = queue.Queue()
    session._free_flushers.put(flusher)
    session._finishers = None
    try:
        result = session.finish(scrape_id)
    finally:
        flusher.shutdown()

    assert result is not None
    assert saved == [(scrape_id, "A")]
    assert rows.length(scrape_id) == 0
    assert database.finished == [scrape_id]


def test_each_keyword_is_its_own_scrape(monkeypatch, redis):
    env = _arrange(monkeypatch, lambda kw, slot: done(kw))
    result = PlaceScraper(redis).scrape(_query(keywords=["cafes", "bakeries"]))
    assert [kw for kw, _ in env.searched] == ["cafes", "bakeries"]
    assert len(result.rows) == 2
    assert [s["settings"]["keywords"] for s in env.scrapes.values()] == [
        ["cafes"],
        ["bakeries"],
    ]


def test_a_lead_kept_by_the_first_keyword_is_not_kept_again(monkeypatch, redis):
    _arrange(monkeypatch, lambda kw, slot: Reply(rows=[row("same", place_id="p1")]))
    result = PlaceScraper(redis).scrape(_query(keywords=["cafes", "bakeries"]))
    assert len(result.rows) == 1


def test_a_lead_filtered_out_by_the_first_keyword_is_kept_for_the_second(
    monkeypatch, redis
):
    def behave(kw, slot):
        phone = "" if kw == "cafes" else "512-555-0123"
        return Reply(rows=[row("same", place_id="p1", phone=phone)])

    _arrange(monkeypatch, behave)
    result = PlaceScraper(redis).scrape(
        _query(keywords=["cafes", "bakeries"], has_phone=True)
    )
    assert [r["phone"] for r in result.rows] == ["+15125550123"]


def test_the_queue_formats_a_local_phone_as_e164_for_the_country(monkeypatch, redis):
    _arrange(
        monkeypatch,
        lambda kw, slot: Reply(rows=[row("Cafe", phone="512-555-0123")]),
    )
    result = PlaceScraper(redis).scrape(_query(keywords=["cafes"], country="us"))
    assert [r["phone"] for r in result.rows] == ["+15125550123"]


def test_the_queue_stores_none_for_a_phone_the_country_cannot_validate(
    monkeypatch, redis
):
    _arrange(
        monkeypatch,
        lambda kw, slot: Reply(rows=[row("Cafe", phone="not a phone")]),
    )
    result = PlaceScraper(redis).scrape(_query(keywords=["cafes"], country="us"))
    assert [r["phone"] for r in result.rows] == [None]


def test_the_phone_filter_drops_a_lead_whose_phone_is_invalid(monkeypatch, redis):
    _arrange(
        monkeypatch,
        lambda kw, slot: Reply(rows=[row("Cafe", phone="not a phone")]),
    )
    result = PlaceScraper(redis).scrape(
        _query(keywords=["cafes"], country="us", has_phone=True)
    )
    assert result.rows == []


def test_a_deleted_keyword_scrape_is_skipped_and_the_next_one_runs(monkeypatch, redis):
    env = _arrange(
        monkeypatch,
        lambda kw, slot: Reply(deleted=True) if kw == "cafes" else done(kw),
    )
    result = PlaceScraper(redis).scrape(_query(keywords=["cafes", "bakeries"]))
    assert [kw for kw, _ in env.searched] == ["cafes", "bakeries"]
    assert result.skipped == 1


def test_rate_limit_stops_the_remaining_keywords(monkeypatch, redis):
    env = _arrange(monkeypatch, lambda kw, slot: Reply(rows=[row("a")], blocked=True))
    result = PlaceScraper(redis).scrape(_query(keywords=["cafes", "bakeries"]))
    assert [kw for kw, _ in env.searched] == ["cafes"]
    assert result.blocked


def test_a_redis_failure_stops_the_keyword_loop(monkeypatch, redis):
    from app.services.buffer.client import BufferError

    def behave(kw, slot):
        raise BufferError("redis down")

    env = _arrange(monkeypatch, behave)
    with pytest.raises(BufferError):
        PlaceScraper(redis).scrape(_query(keywords=["cafes", "bakeries"]))
    assert [kw for kw, _ in env.searched] == ["cafes"]


def test_unsaved_leads_stop_the_keyword_loop(monkeypatch, redis):
    env = _arrange(monkeypatch, lambda kw, slot: Reply(unsaved=True))
    with pytest.raises(UnsavedLeadsError):
        PlaceScraper(redis).scrape(_query(keywords=["cafes", "bakeries"]))
    assert [kw for kw, _ in env.searched] == ["cafes"]


def test_grid_runs_one_scrape_per_tile_for_each_keyword(monkeypatch, redis):
    env = _arrange(monkeypatch, lambda kw, slot: done())
    PlaceScraper(redis).scrape(_query(keywords=["cafes", "bakeries"], grid=2))
    keywords = [kw for kw, _ in env.searched]
    assert keywords.count("cafes") == 4
    assert keywords.count("bakeries") == 4
    assert keywords[:4] == ["cafes"] * 4


def test_a_postgres_error_stops_the_keyword_loop(monkeypatch, redis):
    from app.services.google_map_scraper.scrape.scrape_database import (
        ScrapeDatabaseError,
    )

    def behave(kw, slot):
        raise ScrapeDatabaseError("postgres down")

    env = _arrange(monkeypatch, behave)
    with pytest.raises(ScrapeDatabaseError):
        PlaceScraper(redis).scrape(_query(keywords=["cafes", "bakeries"]))
    assert [kw for kw, _ in env.searched] == ["cafes"]


class _FakeDatabase:
    def __init__(self, *_):
        self.finished = []
        self.checked = 0

    def check_connection(self):
        self.checked += 1

    def requeue_interrupted(self, max_resumes):
        return 0, 0

    def pending_ids(self):
        return []

    def get(self, scrape_id):
        return {"ID": scrape_id}

    def finish(self, scrape_id, status, note, progress):
        self.finished.append(scrape_id)
        return True

    def close(self):
        pass


class _FakeFlusher:
    def __init__(self, *_, prepare_batch=None, **__):
        self.prepare_batch = prepare_batch

    def start(self):
        pass

    def shutdown(self, wait=None):
        pass


class _FakeWorker(threading.Thread):
    """Reports each id it is assigned as finished with no leads."""

    def __init__(
        self,
        database,
        redis,
        headless=True,
        known_keys=frozenset(),
        on_finished: Callable[[str, RunOutcome, list[LeadRow]], None] | None = None,
        assignments: queue.Queue[str] | None = None,
        browser=None,
        proxy_slot=0,
    ):
        super().__init__(daemon=True)
        self.failure = None
        self.browser = browser
        self._assignments = assignments
        self._on_finished = on_finished
        self._stopped = threading.Event()

    def run(self):
        while not self._stopped.is_set():
            if self._assignments is None:
                continue
            try:
                scrape_id = self._assignments.get(timeout=0.05)
            except queue.Empty:
                continue
            if self._on_finished is not None:
                self._on_finished(scrape_id, RunOutcome(ScrapeStatus.OK, ""), [])

    def cancel(self, scrape_id):
        pass

    def shutdown(self, wait=0):
        self._stopped.set()
        if wait and self.is_alive():
            self.join(wait)


class _SessionEnv:
    def __init__(self, monkeypatch, redis):
        monkeypatch.setattr(scraper_module, "ScrapeDatabase", _FakeDatabase)
        monkeypatch.setattr(scraper_module, "Flusher", _FakeFlusher)
        monkeypatch.setattr(scraper_module, "ScrapeWorker", _FakeWorker)
        self._redis = redis
        self.sessions = []

    def session(self, slots, country=""):
        session = scraper_module.ScrapeSession(
            EngineConfig(),
            self._redis,
            cast(TortoiseConfig, None),
            frozenset(),
            slots,
            locale="en-US",
            country=country,
        )
        self.sessions.append(session)
        return session


@pytest.fixture
def fake_session_env(monkeypatch, redis):
    env = _SessionEnv(monkeypatch, redis)
    yield env
    for session in env.sessions:
        session.stop()


def test_session_checks_the_database_before_it_starts(fake_session_env):
    session = fake_session_env.session(slots=1)
    session.start()
    assert session._database.checked == 1


def test_the_session_formats_buffered_phones_before_they_are_saved(fake_session_env):
    session = fake_session_env.session(slots=1, country="us")
    rows = [
        {"phone": "512-555-0123", "website": ""},
        {"phone": "not a phone", "website": ""},
    ]
    session._flushers[0].prepare_batch(rows)
    assert [row["phone"] for row in rows] == ["+15125550123", None]


def test_session_runs_each_submitted_tile_on_its_slot(fake_session_env):
    session = fake_session_env.session(slots=2)
    session.start()
    session.submit("tile-a", 0)
    session.submit("tile-b", 1)
    finished = {
        session.next_finished(timeout=2)[0],
        session.next_finished(timeout=2)[0],
    }
    assert finished == {"tile-a", "tile-b"}


def test_single_slot_session_gives_its_worker_no_shared_browser(fake_session_env):
    session = fake_session_env.session(slots=1)
    assert session._workers[0].browser is None


def test_two_slot_session_attaches_every_worker_to_one_shared_browser(
    fake_session_env,
):
    session = fake_session_env.session(slots=2)
    browsers = [worker.browser for worker in session._workers]
    assert all(browser is not None for browser in browsers)
    shared = {browser._shared for browser in browsers}
    assert len(shared) == 1 and None not in shared
