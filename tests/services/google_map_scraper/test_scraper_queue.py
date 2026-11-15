import fakeredis
import pytest

from app.services.buffer.client import BufferError
from app.services.google_map_scraper import scraper as scraper_module
from app.services.google_map_scraper.place_query import PlaceQuery
from app.services.google_map_scraper.scrape.scrape_status import ScrapeStatus
from app.services.google_map_scraper.scrape.scrape_types import RunResult
from app.services.google_map_scraper.scraper import PlaceScraper


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


def _grid_geocode(*a, **k):
    return ((1.0, 2.0), ["0", "1", "0", "1"])


@pytest.fixture
def redis():
    return fakeredis.FakeRedis(decode_responses=True)


def test_a_crash_in_a_cell_resumes_at_that_cell(monkeypatch, redis):
    calls = []

    def fake_run(name, settings, **_):
        calls.append(settings.latitude)
        if len(calls) == 2:
            raise BufferError("redis down")
        return RunResult("id", ScrapeStatus.OK, "", [], False)

    monkeypatch.setattr(scraper_module, "run_scrape", fake_run)
    monkeypatch.setattr(scraper_module, "get_geocode", _grid_geocode)
    with pytest.raises(BufferError):
        PlaceScraper(redis).scrape(_query(grid=2, keywords=["cafes"]))

    calls.clear()

    def second_run(name, settings, **_):
        calls.append(settings.latitude)
        return RunResult("id", ScrapeStatus.OK, "", [], False)

    monkeypatch.setattr(scraper_module, "run_scrape", second_run)
    PlaceScraper(redis).scrape(_query(grid=2, keywords=["cafes"]))
    assert len(calls) == 3  # cell 2 runs again, then cells 3 and 4


def test_finished_keywords_are_not_searched_again_on_resume(monkeypatch, redis):
    names = []
    crashed = []

    def fake_run(name, settings, **_):
        names.append(settings.keywords[0])
        if settings.keywords[0] == "bakeries" and not crashed:
            crashed.append(True)
            raise BufferError("redis down")
        return RunResult("id", ScrapeStatus.OK, "", [], False)

    monkeypatch.setattr(scraper_module, "run_scrape", fake_run)
    monkeypatch.setattr(scraper_module, "get_geocode", lambda *a, **k: (1.0, 2.0))
    with pytest.raises(BufferError):
        PlaceScraper(redis).scrape(_query(keywords=["cafes", "bakeries"]))
    names.clear()
    monkeypatch.setattr(scraper_module, "run_scrape", fake_run)
    PlaceScraper(redis).scrape(_query(keywords=["cafes", "bakeries"]))
    assert names == ["bakeries"]


def test_a_rate_limit_ends_the_run_and_clears_the_queue(monkeypatch, redis):
    monkeypatch.setattr(
        scraper_module,
        "run_scrape",
        lambda n, s, **_: RunResult("id", ScrapeStatus.OK, "", [_row()], True),
    )
    monkeypatch.setattr(scraper_module, "get_geocode", lambda *a, **k: (1.0, 2.0))
    result = PlaceScraper(redis).scrape(_query(keywords=["cafes", "bakeries"]))
    assert result.blocked
    assert not redis.keys("place_queue:*")


def test_a_run_with_one_job_per_keyword_returns_rows_in_keyword_order(monkeypatch, redis):
    monkeypatch.setattr(
        scraper_module,
        "run_scrape",
        lambda n, s, **_: RunResult(
            "id", ScrapeStatus.OK, "", [_row(title=s.keywords[0])], False
        ),
    )
    monkeypatch.setattr(scraper_module, "get_geocode", lambda *a, **k: (1.0, 2.0))
    result = PlaceScraper(redis).scrape(_query(keywords=["cafes", "bakeries"]))
    assert [row["title"] for row in result.rows] == ["cafes", "bakeries"]
    assert not redis.keys("place_queue:*")


def test_a_lost_chrome_connection_ends_the_run_and_clears_the_queue(monkeypatch, redis):
    from app.services.browser import CDPError

    names = []

    def fake_run(name, settings, **_):
        names.append(settings.keywords[0])
        raise CDPError("gone")

    monkeypatch.setattr(scraper_module, "run_scrape", fake_run)
    monkeypatch.setattr(scraper_module, "get_geocode", lambda *a, **k: (1.0, 2.0))
    result = PlaceScraper(redis).scrape(_query(keywords=["cafes", "bakeries"]))
    assert names == ["cafes"]
    assert result.skipped == 1
    assert not redis.keys("place_queue:*")


def test_a_geocoding_failure_leaves_no_queue(monkeypatch, redis):
    monkeypatch.setattr(scraper_module, "get_geocode", lambda *a, **k: None)
    result = PlaceScraper(redis).scrape(_query(keywords=["cafes"]))
    assert result.rows == []
    assert not redis.keys("place_queue:*")


def test_a_job_exception_is_raised_in_the_caller_and_keeps_the_queue(monkeypatch, redis):
    def fake_run(name, settings, **_):
        raise BufferError("redis down")

    monkeypatch.setattr(scraper_module, "run_scrape", fake_run)
    monkeypatch.setattr(scraper_module, "get_geocode", lambda *a, **k: (1.0, 2.0))
    with pytest.raises(BufferError):
        PlaceScraper(redis).scrape(_query(keywords=["cafes"]))
    assert redis.keys("place_queue:*")


def test_ctrl_c_stops_the_job_before_the_next_cell_and_keeps_the_queue(monkeypatch, redis):
    scraper = PlaceScraper(redis)
    calls = []

    def fake_run(name, settings, **_):
        calls.append(settings.latitude)
        scraper._cancel.set()  # what the listener does when Ctrl-C reaches its join
        return RunResult("id", ScrapeStatus.OK, "", [_row(latitude=0.5, longitude=0.5)], False)

    monkeypatch.setattr(scraper_module, "run_scrape", fake_run)
    monkeypatch.setattr(scraper_module, "get_geocode", _grid_geocode)
    with pytest.raises(KeyboardInterrupt):
        scraper.scrape(_query(grid=2, keywords=["cafes"]))
    assert len(calls) == 1
    assert redis.keys("place_queue:*")
    assert len(scraper.salvage_rows) == 1


def test_a_listener_interrupt_sets_the_cancel_event_and_waits_for_the_job(monkeypatch, redis):
    import threading

    scraper = PlaceScraper(redis)
    real_join = threading.Thread.join
    gate = threading.Event()
    started = threading.Event()
    interrupted = []
    calls = []

    def join_once_interrupted(self, timeout=None):
        if self.name.startswith("place-job-"):
            if not interrupted:
                interrupted.append(True)
                assert started.wait(timeout=5)  # the first cell is in flight
                raise KeyboardInterrupt()
            assert scraper._cancel.is_set()
            gate.set()  # the first cell may only finish after the cancel is set
        return real_join(self, timeout)

    def fake_run(name, settings, **_):
        calls.append(settings.latitude)
        started.set()
        assert gate.wait(timeout=5)
        return RunResult("id", ScrapeStatus.OK, "", [], False)

    monkeypatch.setattr(threading.Thread, "join", join_once_interrupted)
    monkeypatch.setattr(scraper_module, "run_scrape", fake_run)
    monkeypatch.setattr(scraper_module, "get_geocode", _grid_geocode)
    with pytest.raises(KeyboardInterrupt):
        scraper.scrape(_query(grid=2, keywords=["cafes"]))
    assert scraper._cancel.is_set()
    assert len(calls) == 1  # the job stopped at its next cell instead of running on
    assert redis.keys("place_queue:*")


def test_a_failed_enqueue_leaves_no_queue(monkeypatch, redis):
    from app.services.google_map_scraper.place_queue import PlaceJobQueue

    real_enqueue = PlaceJobQueue.enqueue
    count = []

    def enqueue_then_fail(self, job):
        if count:
            raise BufferError("redis down")
        count.append(True)
        real_enqueue(self, job)

    monkeypatch.setattr(PlaceJobQueue, "enqueue", enqueue_then_fail)
    monkeypatch.setattr(scraper_module, "get_geocode", lambda *a, **k: (1.0, 2.0))
    with pytest.raises(BufferError):
        PlaceScraper(redis).scrape(_query(keywords=["cafes", "bakeries"]))
    assert not redis.keys("place_queue:*")


def test_a_failed_clear_after_a_failed_enqueue_keeps_the_original_error(monkeypatch, redis):
    from app.services.google_map_scraper.place_queue import PlaceJobQueue

    def enqueue_fails(self, job):
        raise BufferError("enqueue down")

    def clear_fails(self):
        raise BufferError("clear down")

    monkeypatch.setattr(PlaceJobQueue, "enqueue", enqueue_fails)
    monkeypatch.setattr(PlaceJobQueue, "clear", clear_fails)
    monkeypatch.setattr(scraper_module, "get_geocode", lambda *a, **k: (1.0, 2.0))
    with pytest.raises(BufferError, match="enqueue down"):
        PlaceScraper(redis).scrape(_query(keywords=["cafes"]))
