"""Monotonic deadlines that bound every step of a render."""

import time

from .constants import LAUNCH_RESERVE_SECONDS, MIN_REMAINING_SECONDS


def seconds_left(deadline: float) -> float:
    """Return the seconds remaining until ``deadline`` (a ``time.monotonic()`` value)."""
    return deadline - time.monotonic()


def milliseconds_left(deadline: float) -> int:
    """Return the time left as a Playwright timeout, in milliseconds.

    :raises TimeoutError: If less than :data:`MIN_REMAINING_SECONDS` remain.
    """
    remaining = seconds_left(deadline)
    if remaining < float(MIN_REMAINING_SECONDS):
        raise TimeoutError("browser budget exhausted")
    return int((remaining - LAUNCH_RESERVE_SECONDS) * 1000)
