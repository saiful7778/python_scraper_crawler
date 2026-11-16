import re
from datetime import UTC, datetime, timedelta

from app.services.buffer.crawl_buffer import _restore
from app.utils.utils import isoformat_or_none, utc_now


def test_utc_now_is_iso_utc_timestamp_with_microseconds() -> None:
    value = utc_now()
    parsed = datetime.fromisoformat(value)
    assert parsed.utcoffset() == timedelta(0)
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{6})?\+00:00", value)


def test_isoformat_or_none_keeps_none() -> None:
    assert isoformat_or_none(None) is None


def test_isoformat_or_none_formats_a_datetime() -> None:
    moment = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
    assert isoformat_or_none(moment) == "2026-01-02T03:04:05+00:00"


def test_restore_still_reads_second_precision_timestamps() -> None:
    row = _restore({"created_at": "2026-01-02T03:04:05+00:00", "id": "x"})
    assert row["created_at"] == datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
