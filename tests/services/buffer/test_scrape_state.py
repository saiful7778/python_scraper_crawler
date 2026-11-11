import pytest

from app.services.buffer.client import BufferError
from app.services.buffer.keys import (
    active_key,
    known_key,
    processing_key,
    queue_key,
    queued_key,
    rows_key,
    state_key,
)
from app.services.buffer.scrape_state import ScrapeState
from app.services.google_map_scraper.scrape import scrape_limits

SETTINGS = {"keyword": "plumber", "city": "Austin"}


@pytest.fixture
def state(fake_redis):
    return ScrapeState(fake_redis, ttl_seconds=3600)


def test_create_stores_settings_pending_status_and_active_id(state, fake_redis):
    state.create("s1", SETTINGS)

    assert state.get_settings("s1") == SETTINGS
    assert fake_redis.hget(state_key("s1"), "status") == "pending"
    assert fake_redis.sismember(active_key(), "s1") == 1


def test_create_expires_the_active_set(state, fake_redis):
    state.create("s1", SETTINGS)

    assert 0 < fake_redis.ttl(active_key()) <= 3600


def test_get_settings_returns_none_for_unknown_scrape(state):
    assert state.get_settings("missing") is None


def test_progress_round_trips(state):
    state.create("s1", SETTINGS)
    state.save_progress("s1", {"done": 2, "stage": "search"})

    assert state.get_progress("s1") == {"done": 2, "stage": "search"}


def test_get_progress_is_empty_before_any_save(state):
    state.create("s1", SETTINGS)

    assert state.get_progress("s1") == {}


def test_set_status_updates_status(state, fake_redis):
    state.create("s1", SETTINGS)
    state.set_status("s1", "running")

    assert fake_redis.hget(state_key("s1"), "status") == "running"


def test_get_progress_raises_buffer_error_on_corrupt_json(state, fake_redis):
    state.create("s1", SETTINGS)
    fake_redis.hset(state_key("s1"), "progress", "{not json")

    with pytest.raises(BufferError):
        state.get_progress("s1")


def test_write_methods_set_ttl_on_state_key(state, fake_redis):
    state.create("s1", SETTINGS)
    assert 0 < fake_redis.ttl(state_key("s1")) <= 3600

    fake_redis.persist(state_key("s1"))
    state.save_progress("s1", {"done": 1})
    assert 0 < fake_redis.ttl(state_key("s1")) <= 3600

    fake_redis.persist(state_key("s1"))
    state.set_status("s1", "running")
    assert 0 < fake_redis.ttl(state_key("s1")) <= 3600


def test_refresh_ttl_expires_every_scrape_key(state, fake_redis):
    state.create("s1", SETTINGS)
    other_keys = [
        rows_key("s1"),
        known_key("s1"),
        queue_key("s1"),
        processing_key("s1"),
        queued_key("s1"),
    ]
    for key in other_keys:
        fake_redis.sadd(key, "x")

    state.refresh_ttl("s1")

    for key in [state_key("s1"), *other_keys]:
        assert 0 < fake_redis.ttl(key) <= 3600


def test_discard_removes_every_key(state, fake_redis):
    state.create("s1", SETTINGS)
    all_keys = [
        rows_key("s1"),
        known_key("s1"),
        queue_key("s1"),
        processing_key("s1"),
        queued_key("s1"),
    ]
    for key in all_keys:
        fake_redis.sadd(key, "x")

    state.discard("s1")

    for key in [state_key("s1"), *all_keys]:
        assert fake_redis.exists(key) == 0
    assert fake_redis.sismember(active_key(), "s1") == 0


def test_discard_keeps_other_scrapes(state, fake_redis):
    state.create("s1", SETTINGS)
    state.create("s2", SETTINGS)

    state.discard("s1")

    assert state.get_settings("s2") == SETTINGS
    assert fake_redis.sismember(active_key(), "s2") == 1


def test_active_ids_lists_the_scrapes_that_exist(state):
    state.create("s1", {})
    state.create("s2", {})
    state.discard("s1")
    assert state.active_ids() == ["s2"]


def _full_settings(**overrides) -> dict:
    settings = {
        "keywords": ["bakery", "cafe"],
        "lang": "de",
        "country": "de",
        "lat": 52.5,
        "lon": 13.4,
        "zoom": 15,
        "depth": 2,
        "max_time": 600,
        "fast_mode": True,
        "email": True,
        "socials": False,
        "proxies": ["http://proxy-a:8080", "http://proxy-b:8080"],
        "limit": 50,
        "crawl_max_pages": 100,
        "crawl_max_depth": 3,
    }
    settings.update(overrides)
    return settings


def test_settings_round_trip(state):
    settings = _full_settings()
    state.create("s1", settings)
    assert state.get_settings("s1") == settings


def test_unset_optional_settings_come_back_as_stored(state):
    settings = _full_settings(lat=None, lon=None, limit=None, proxies=[], keywords=["bar"])
    state.create("s1", settings)
    assert state.get_settings("s1") == settings


def test_keyword_and_proxy_order_is_kept(state):
    settings = _full_settings(keywords=["c", "a", "b"], proxies=["http://z", "http://y"])
    state.create("s1", settings)
    data = state.get_settings("s1")
    assert data["keywords"] == ["c", "a", "b"]
    assert data["proxies"] == ["http://z", "http://y"]


def test_buffer_expiry_is_seven_days():
    assert scrape_limits.BUFFER_TTL_SECONDS == 7 * 24 * 3600
