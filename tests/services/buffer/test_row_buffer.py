from typing import cast

import pytest

from app.services.buffer.client import BufferFullError
from app.services.buffer.keys import active_key, rows_key
from app.services.buffer.row_buffer import RowBuffer
from app.services.google_map_scraper.scrape.scrape_types import LeadRow


def _rows(*titles: str) -> list[LeadRow]:
    return [cast(LeadRow, {"title": title}) for title in titles]


def test_trim_keeps_rows_appended_after_peek(fake_redis):
    buffer = RowBuffer(fake_redis, ttl_seconds=3600)
    buffer.append("s1", _rows("A", "B"))
    batch = buffer.peek("s1", 2)
    buffer.append("s1", _rows("C"))
    buffer.trim("s1", len(batch))
    assert buffer.read_all("s1") == _rows("C")


def test_peek_returns_oldest_rows_without_removing_them(fake_redis):
    buffer = RowBuffer(fake_redis, ttl_seconds=3600)
    buffer.append("s1", _rows("A", "B", "C"))
    assert buffer.peek("s1", 2) == _rows("A", "B")
    assert buffer.length("s1") == 3


def test_length_and_read_all_of_unknown_scrape_are_empty(fake_redis):
    buffer = RowBuffer(fake_redis, ttl_seconds=3600)
    assert buffer.length("missing") == 0
    assert buffer.read_all("missing") == []
    assert buffer.peek("missing", 5) == []


def test_trim_beyond_length_empties_the_buffer(fake_redis):
    buffer = RowBuffer(fake_redis, ttl_seconds=3600)
    buffer.append("s1", _rows("A", "B"))
    buffer.trim("s1", 10)
    assert buffer.length("s1") == 0


def test_append_sets_ttl_and_registers_scrape_as_active(fake_redis):
    buffer = RowBuffer(fake_redis, ttl_seconds=3600)
    buffer.append("s1", _rows("A"))
    assert 0 < fake_redis.ttl(rows_key("s1")) <= 3600
    assert fake_redis.sismember(active_key(), "s1")


def test_append_expires_the_active_set(fake_redis):
    buffer = RowBuffer(fake_redis, ttl_seconds=3600)
    buffer.append("s1", _rows("A"))
    assert 0 < fake_redis.ttl(active_key()) <= 3600


def test_append_of_no_rows_writes_nothing(fake_redis):
    buffer = RowBuffer(fake_redis, ttl_seconds=3600)
    buffer.append("s1", [])
    assert fake_redis.exists(rows_key("s1")) == 0
    assert not fake_redis.sismember(active_key(), "s1")


def test_full_redis_raises_buffer_full_on_append(fake_redis_full):
    buffer = RowBuffer(fake_redis_full, ttl_seconds=3600)
    with pytest.raises(BufferFullError, match="Redis memory limit"):
        buffer.append("s1", _rows("A"))
