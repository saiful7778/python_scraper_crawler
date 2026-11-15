import queue
import threading

import fakeredis
import pytest

from app.services.google_map_scraper import scraper as scraper_module
from app.services.google_map_scraper.place_query import PlaceQuery
from app.services.google_map_scraper.scrape.engine_config import EngineConfig
from app.services.google_map_scraper.scrape.lead_filter import keep_lead
from app.services.google_map_scraper.scrape.scrape_run import RunOutcome
from app.services.google_map_scraper.scrape.scrape_status import ScrapeStatus
from app.services.google_map_scraper.scrape.scrape_types import RunResult
from app.services.google_map_scraper.scraper import (
    BoundingBoxError,
    PlaceScraper,
    ScrapeDeletedError,
    finalize_rows,
    parse_bounding_box,
)


@pytest.fixture
def redis():
    return fakeredis.FakeRedis(decode_responses=True)


def _query(**overrides) -> PlaceQuery:
    data = {"keywords": ["cafe"], "city": "Austin", "country": "us", **overrides}
    return PlaceQuery.model_validate(data)


def _row(**overrides) -> dict:
    return {
        "title": "Cafe",
        "latitude": 30.27,
        "longitude": -97.74,
        "phone": "",
        "website": "",
        **overrides,
    }


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


def test_final_flush_has_no_time_limit():
    from app.services.google_map_scraper import scraper

    assert not hasattr(scraper, "_FINAL_FLUSH_SECONDS")


def test_grid_stops_when_a_tiles_leads_stay_unsaved(monkeypatch, redis):
    from app.services.google_map_scraper import scraper
    from app.services.google_map_scraper.scraper import PlaceScraper, UnsavedLeadsError

    calls = []

    def fake_geocode(place, with_bbox=False, country=None):
        return (30.0, -97.0), ["29.0", "31.0", "-98.0", "-96.0"]

    def fake_run_scrape(name, settings, **kwargs):
        calls.append(settings)
        raise UnsavedLeadsError("s1", 3)

    monkeypatch.setattr(scraper, "get_geocode", fake_geocode)
    monkeypatch.setattr(scraper, "run_scrape", fake_run_scrape)

    with pytest.raises(UnsavedLeadsError):
        PlaceScraper(redis).scrape(_query(grid=2))
    assert len(calls) == 1


def test_unsaved_leads_of_the_scrape_raise(monkeypatch):
    from app.services.google_map_scraper.scraper import UnsavedLeadsError
    from app.utils.errors import ScraperError

    assert issubclass(UnsavedLeadsError, ScraperError)
    error = UnsavedLeadsError("s1", 3)
    assert "3 lead(s)" in str(error)
    assert "s1" in str(error)


def test_each_keyword_is_its_own_scrape(monkeypatch, redis):
    calls = []

    def fake_run(name, settings, *, known_keys=frozenset(), **_):
        calls.append((name, settings.keywords))
        return RunResult(
            "id", ScrapeStatus.OK, "", [_row(title=settings.keywords[0])], False
        )

    monkeypatch.setattr(scraper_module, "run_scrape", fake_run)
    monkeypatch.setattr(scraper_module, "get_geocode", lambda *a, **k: (1.0, 2.0))
    result = PlaceScraper(redis).scrape(_query(keywords=["cafes", "bakeries"]))
    assert [keywords for _, keywords in calls] == [("cafes",), ("bakeries",)]
    assert len(result.rows) == 2


def test_a_lead_kept_by_the_first_keyword_is_known_to_the_second(monkeypatch, redis):
    seen_known = []

    def fake_run(name, settings, *, known_keys=frozenset(), **_):
        seen_known.append(set(known_keys))
        return RunResult("id", ScrapeStatus.OK, "", [_row(place_id="p1")], False)

    monkeypatch.setattr(scraper_module, "run_scrape", fake_run)
    monkeypatch.setattr(scraper_module, "get_geocode", lambda *a, **k: (1.0, 2.0))
    PlaceScraper(redis).scrape(_query(keywords=["cafes", "bakeries"]))
    assert seen_known[1]  # the second scrape received the first scrape's keys


def test_a_deleted_keyword_scrape_is_skipped_and_the_next_one_runs(monkeypatch, redis):
    names = []

    def fake_run(name, settings, **_):
        names.append(settings.keywords[0])
        if settings.keywords[0] == "cafes":
            raise ScrapeDeletedError("deleted")
        return RunResult("id", ScrapeStatus.OK, "", [_row()], False)

    monkeypatch.setattr(scraper_module, "run_scrape", fake_run)
    monkeypatch.setattr(scraper_module, "get_geocode", lambda *a, **k: (1.0, 2.0))
    result = PlaceScraper(redis).scrape(_query(keywords=["cafes", "bakeries"]))
    assert names == ["cafes", "bakeries"]
    assert result.skipped == 1


def test_a_lead_filtered_out_by_the_first_keyword_is_not_known(monkeypatch, redis):
    seen_known = []

    def fake_run(name, settings, *, known_keys=frozenset(), **_):
        seen_known.append(set(known_keys))
        return RunResult(
            "id", ScrapeStatus.OK, "", [_row(place_id="p1", phone="")], False
        )

    monkeypatch.setattr(scraper_module, "run_scrape", fake_run)
    monkeypatch.setattr(scraper_module, "get_geocode", lambda *a, **k: (1.0, 2.0))
    PlaceScraper(redis).scrape(_query(keywords=["cafes", "bakeries"], has_phone=True))
    assert not seen_known[1]  # the phone filter dropped it, so keyword 2 may keep it


def test_rate_limit_stops_the_remaining_keywords(monkeypatch, redis):
    names = []

    def fake_run(name, settings, **_):
        names.append(settings.keywords[0])
        return RunResult("id", ScrapeStatus.OK, "", [_row()], True)

    monkeypatch.setattr(scraper_module, "run_scrape", fake_run)
    monkeypatch.setattr(scraper_module, "get_geocode", lambda *a, **k: (1.0, 2.0))
    result = PlaceScraper(redis).scrape(_query(keywords=["cafes", "bakeries"]))
    assert names == ["cafes"]
    assert result.blocked


def test_a_redis_failure_stops_the_keyword_loop(monkeypatch, redis):
    from app.services.buffer.client import BufferError

    calls = []

    def fake_run(name, settings, **_):
        calls.append(settings.keywords[0])
        raise BufferError("redis down")

    monkeypatch.setattr(scraper_module, "run_scrape", fake_run)
    monkeypatch.setattr(scraper_module, "get_geocode", lambda *a, **k: (1.0, 2.0))
    with pytest.raises(BufferError):
        PlaceScraper(redis).scrape(_query(keywords=["cafes", "bakeries"]))
    assert calls == ["cafes"]


def test_unsaved_leads_stop_the_keyword_loop(monkeypatch, redis):
    from app.services.google_map_scraper.scraper import UnsavedLeadsError

    calls = []

    def fake_run(name, settings, **_):
        calls.append(settings.keywords[0])
        raise UnsavedLeadsError("s1", 3)

    monkeypatch.setattr(scraper_module, "run_scrape", fake_run)
    monkeypatch.setattr(scraper_module, "get_geocode", lambda *a, **k: (1.0, 2.0))
    with pytest.raises(UnsavedLeadsError):
        PlaceScraper(redis).scrape(_query(keywords=["cafes", "bakeries"]))
    assert calls == ["cafes"]


def test_grid_runs_one_scrape_per_tile_for_each_keyword(monkeypatch, redis):
    names = []

    def fake_run(name, settings, **_):
        names.append((settings.keywords[0], settings.latitude))
        return RunResult("id", ScrapeStatus.OK, "", [], False)

    monkeypatch.setattr(scraper_module, "run_scrape", fake_run)
    monkeypatch.setattr(
        scraper_module,
        "get_geocode",
        lambda *a, **k: ((1.0, 2.0), ["0", "1", "0", "1"]),
    )
    PlaceScraper(redis).scrape(_query(keywords=["cafes", "bakeries"], grid=2))
    keywords = [keyword for keyword, _ in names]
    assert keywords.count("cafes") == 4
    assert keywords.count("bakeries") == 4
    assert keywords[:4] == ["cafes"] * 4


def test_limit_applies_to_each_tile_not_the_whole_grid(monkeypatch, redis):
    calls = []

    def fake_run(name, settings, **_):
        calls.append(name)
        tile = len(calls)
        return RunResult(
            "id",
            ScrapeStatus.OK,
            "",
            [
                _row(title=f"tile{tile}-a", latitude=0.5, longitude=0.5),
                _row(title=f"tile{tile}-b", latitude=0.5, longitude=0.5),
            ],
            False,
        )

    monkeypatch.setattr(scraper_module, "run_scrape", fake_run)
    monkeypatch.setattr(
        scraper_module,
        "get_geocode",
        lambda *a, **k: ((1.0, 2.0), ["0", "1", "0", "1"]),
    )
    result = PlaceScraper(redis).scrape(_query(grid=2, limit=1, keywords=["cafes"]))
    assert len(calls) == 4
    assert len(result.rows) == 4


def _grid_geocode(*a, **k):
    return ((1.0, 2.0), ["0", "1", "0", "1"])


def test_a_failed_grid_tiles_rows_are_kept(monkeypatch, redis):
    calls = []

    def fake_run(name, settings, **_):
        calls.append(settings.latitude)
        if len(calls) == 1:
            return RunResult(
                "id",
                ScrapeStatus.FAILED,
                "boom",
                [_row(title="failed-tile", latitude=0.5, longitude=0.5)],
                False,
            )
        return RunResult("id", ScrapeStatus.OK, "", [], False)

    monkeypatch.setattr(scraper_module, "run_scrape", fake_run)
    monkeypatch.setattr(scraper_module, "get_geocode", _grid_geocode)
    result = PlaceScraper(redis).scrape(_query(grid=2, keywords=["cafes"]))
    assert "failed-tile" in [row["title"] for row in result.rows]


def test_a_blocked_grid_tiles_rows_are_kept(monkeypatch, redis):
    def fake_run(name, settings, **_):
        return RunResult(
            "id",
            ScrapeStatus.OK,
            "",
            [_row(title="blocked-tile", latitude=0.5, longitude=0.5)],
            True,
        )

    monkeypatch.setattr(scraper_module, "run_scrape", fake_run)
    monkeypatch.setattr(scraper_module, "get_geocode", _grid_geocode)
    result = PlaceScraper(redis).scrape(_query(grid=2, keywords=["cafes"]))
    assert result.blocked
    assert "blocked-tile" in [row["title"] for row in result.rows]


def test_a_postgres_error_stops_the_keyword_loop(monkeypatch, redis):
    from app.services.google_map_scraper.scrape.scrape_database import (
        ScrapeDatabaseError,
    )

    calls = []

    def fake_run(name, settings, **_):
        calls.append(settings.keywords[0])
        raise ScrapeDatabaseError("postgres down")

    monkeypatch.setattr(scraper_module, "run_scrape", fake_run)
    monkeypatch.setattr(scraper_module, "get_geocode", lambda *a, **k: (1.0, 2.0))
    with pytest.raises(ScrapeDatabaseError):
        PlaceScraper(redis).scrape(_query(keywords=["cafes", "bakeries"]))
    assert calls == ["cafes"]


class _FakeDatabase:
    def __init__(self, *_):
        self.finished = []

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
    def __init__(self, *_, **__):
        pass

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
        on_finished=None,
        assignments=None,
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
            try:
                scrape_id = self._assignments.get(timeout=0.05)
            except queue.Empty:
                continue
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

    def session(self, slots):
        session = scraper_module.ScrapeSession(
            EngineConfig(),
            self._redis,
            None,
            frozenset(),
            slots,
            locale="en-US",
        )
        self.sessions.append(session)
        return session


@pytest.fixture
def fake_session_env(monkeypatch, redis):
    env = _SessionEnv(monkeypatch, redis)
    yield env
    for session in env.sessions:
        session.stop()


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
