from datetime import UTC, datetime


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def isoformat_or_none(value: datetime | None) -> str | None:
    return None if value is None else value.isoformat()
