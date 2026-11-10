"""Per-host request spacing shared across threads."""

import threading
from typing import Protocol

from .timing import (
    SYSTEM_CLOCK,
    Clock,
    interruptible_sleep,
    raise_if_cancelled,
)


class RateLimit(Protocol):
    def wait(self, host: str, cancel_event: threading.Event | None = None) -> None:
        """Block until a request to ``host`` may be sent."""
        ...


def reserve_slot(
    now: float, next_free: float | None, delay: float
) -> tuple[float, float]:
    """Return ``(slot, new_next_free)`` for a request that arrives at ``now``."""
    slot = now if next_free is None else max(now, next_free)
    return slot, slot + delay


class HostPacer:
    """Spaces out requests to one host by at least ``delay`` seconds across threads."""

    def __init__(self, delay: float, clock: Clock = SYSTEM_CLOCK):
        self._delay = max(0.0, delay)
        self._clock = clock
        self._next_free: dict[str, float] = {}
        self._lock = threading.Lock()

    def wait(self, host: str, cancel_event: threading.Event | None = None) -> None:
        """Block until this thread may send its request to ``host``."""
        raise_if_cancelled(cancel_event)
        if self._delay <= 0:
            return
        with self._lock:
            now = self._clock.monotonic()
            slot, self._next_free[host] = reserve_slot(
                now, self._next_free.get(host), self._delay
            )
        interruptible_sleep(slot - now, cancel_event, self._clock)
