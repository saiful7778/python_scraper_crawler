from datetime import datetime, timezone
import sys


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def log(msg: object) -> None:
    try:
        print(f"{utc_now()} {msg}", file=sys.stderr, flush=True)
    except (OSError, ValueError):
        pass
