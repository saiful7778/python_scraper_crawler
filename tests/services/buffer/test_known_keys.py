from typing import cast

import pytest

from app.services.buffer.client import BufferFullError
from app.services.buffer.keys import known_key
from app.services.buffer.known_keys import KnownKeys
from app.services.google_map_scraper.scrape.scrape_types import LeadRow


def lead(**fields: object) -> LeadRow:
    """A row holding only the fields a test cares about."""
    return cast(LeadRow, fields)


def test_add_if_new_rejects_a_known_title(fake_redis):
    keys = KnownKeys(fake_redis, ttl_seconds=3600)
    keys.load("s1", ["title:Cafe A"])
    assert keys.add_if_new("s1", lead(title="Cafe A")) is False


def test_add_if_new_accepts_then_rejects_the_repeat(fake_redis):
    keys = KnownKeys(fake_redis, ttl_seconds=3600)
    assert keys.add_if_new("s1", lead(title="Cafe B")) is True
    assert keys.add_if_new("s1", lead(title="Cafe B")) is False


def test_add_if_new_rejects_a_row_sharing_any_key(fake_redis):
    keys = KnownKeys(fake_redis, ttl_seconds=3600)
    keys.load("s1", ["place_id:p1"])
    assert keys.add_if_new("s1", lead(title="Cafe C", place_id="p1")) is False
    assert fake_redis.sismember(known_key("s1"), "title:Cafe C") == 0


def test_add_if_new_accepts_a_row_with_no_keys(fake_redis):
    keys = KnownKeys(fake_redis, ttl_seconds=3600)
    assert keys.add_if_new("s1", lead()) is True


def test_add_if_new_many_accepts_each_unknown_row(fake_redis):
    keys = KnownKeys(fake_redis, ttl_seconds=3600)
    rows = [lead(title="Cafe A"), lead(title="Cafe B")]
    assert keys.add_if_new_many("s1", rows) == [0, 1]
    assert fake_redis.sismember(known_key("s1"), "title:Cafe A") == 1


def test_add_if_new_many_rejects_known_and_repeated_rows(fake_redis):
    keys = KnownKeys(fake_redis, ttl_seconds=3600)
    keys.load("s1", ["place_id:p1"])
    rows = [
        lead(title="Cafe A"),
        lead(title="Cafe A"),
        lead(title="Cafe B", place_id="p1"),
    ]
    assert keys.add_if_new_many("s1", rows) == [0]
    assert fake_redis.sismember(known_key("s1"), "title:Cafe B") == 0


def test_add_if_new_many_accepts_rows_with_no_keys(fake_redis):
    keys = KnownKeys(fake_redis, ttl_seconds=3600)
    assert keys.add_if_new_many("s1", [lead(), lead()]) == [0, 1]


def test_load_returns_the_set_size_across_batches(fake_redis):
    keys = KnownKeys(fake_redis, ttl_seconds=3600)
    assert keys.load("s1", (f"title:{n}" for n in range(2500))) == 2500
    assert keys.load("s1", ["title:0", "title:extra"]) == 2501


def test_load_with_no_keys_returns_the_current_size(fake_redis):
    keys = KnownKeys(fake_redis, ttl_seconds=3600)
    assert keys.load("s1", []) == 0


def test_load_sets_the_ttl(fake_redis):
    KnownKeys(fake_redis, ttl_seconds=3600).load("s1", ["title:x"])
    assert 0 < fake_redis.ttl(known_key("s1")) <= 3600


def test_add_if_new_sets_the_ttl(fake_redis):
    KnownKeys(fake_redis, ttl_seconds=3600).add_if_new("s1", lead(title="Cafe D"))
    assert 0 < fake_redis.ttl(known_key("s1")) <= 3600


def test_discard_deletes_the_set(fake_redis):
    keys = KnownKeys(fake_redis, ttl_seconds=3600)
    keys.load("s1", ["title:x"])
    keys.discard("s1")
    assert fake_redis.exists(known_key("s1")) == 0


def test_shared_baseline_is_not_copied_into_the_tile_set(fake_redis):
    shared = "place_known:run"
    KnownKeys(fake_redis, ttl_seconds=3600, shared_key=shared).load(
        shared, ["title:Cafe A"]
    )
    keys = KnownKeys(fake_redis, ttl_seconds=3600, shared_key=shared)
    assert keys.add_if_new("s1", lead(title="Cafe A")) is False
    assert fake_redis.scard(known_key("s1")) == 0


def test_shared_baseline_is_checked_by_add_if_new_many(fake_redis):
    shared = "place_known:run"
    KnownKeys(fake_redis, ttl_seconds=3600, shared_key=shared).load(
        shared, ["place_id:p1"]
    )
    keys = KnownKeys(fake_redis, ttl_seconds=3600, shared_key=shared)
    rows = [lead(title="Cafe A"), lead(title="Cafe B", place_id="p1")]
    assert keys.add_if_new_many("s1", rows) == [0]
    assert fake_redis.scard(known_key("s1")) == 1


def test_tile_discoveries_still_dedupe_within_the_tile(fake_redis):
    keys = KnownKeys(fake_redis, ttl_seconds=3600, shared_key="place_known:run")
    assert keys.add_if_new("s1", lead(title="Cafe B")) is True
    assert keys.add_if_new("s1", lead(title="Cafe B")) is False
    assert fake_redis.scard(known_key("s1")) == 1


def test_forget_leaves_the_shared_baseline_alone(fake_redis):
    shared = "place_known:run"
    KnownKeys(fake_redis, ttl_seconds=3600, shared_key=shared).load(
        shared, ["title:Cafe A"]
    )
    keys = KnownKeys(fake_redis, ttl_seconds=3600, shared_key=shared)
    keys.forget("s1", lead(title="Cafe A"))
    assert fake_redis.sismember(shared, "title:Cafe A") == 1


def test_load_sets_the_ttl_of_the_shared_baseline(fake_redis):
    shared = "place_known:run"
    keys = KnownKeys(fake_redis, ttl_seconds=3600, shared_key=shared)
    keys.load(shared, ["title:x"])
    assert 0 < fake_redis.ttl(shared) <= 3600


def test_load_tile_keeps_a_resumed_tiles_own_keys_out_of_the_shared_set(fake_redis):
    shared = "place_known:run"
    keys = KnownKeys(fake_redis, ttl_seconds=3600, shared_key=shared)
    keys.load_tile("s1", ["title:Cafe A"])
    assert fake_redis.sismember(known_key("s1"), "title:Cafe A") == 1
    assert fake_redis.exists(shared) == 0


def test_full_redis_raises_buffer_full(fake_redis_full):
    keys = KnownKeys(fake_redis_full, ttl_seconds=3600)
    with pytest.raises(BufferFullError, match="500mb"):
        keys.load("s1", ["title:x"])


def test_full_redis_raises_buffer_full_on_add_if_new(fake_redis_full):
    keys = KnownKeys(fake_redis_full, ttl_seconds=3600)
    with pytest.raises(BufferFullError, match="Redis memory limit"):
        keys.add_if_new("s1", lead(title="Cafe E"))
