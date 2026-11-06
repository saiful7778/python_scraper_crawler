import sys
from datetime import UTC, datetime


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def log(msg: object) -> None:
    try:
        print(f"{utc_now()} {msg}", file=sys.stderr, flush=True)
    except (OSError, ValueError):
        pass
