"""Clocks, deadlines and cancellable waits.

Everything that reads the time goes through a :class:`Clock`, so tests can
swap in a fake one instead of sleeping.
"""

import threading
import time
from dataclasses import dataclass
from typing import Protocol, Self

from .errors import FetchCancelled


class Clock(Protocol):
    """Source of time; swap it out to test timing without waiting."""

    def monotonic(self) -> float: ...

    def time(self) -> float: ...

    def sleep(self, seconds: float) -> None: ...


class SystemClock:
    def monotonic(self) -> float:
        return time.monotonic()

    def time(self) -> float:
        return time.time()

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)


SYSTEM_CLOCK = SystemClock()


@dataclass(frozen=True)
class Deadline:
    """A point in time after which a fetch must stop."""

    expires_at: float
    clock: Clock = SYSTEM_CLOCK

    @classmethod
    def after(cls, seconds: float, clock: Clock = SYSTEM_CLOCK) -> Self:
        return cls(clock.monotonic() + seconds, clock)

    def remaining(self) -> float:
        return max(0.0, self.expires_at - self.clock.monotonic())

    @property
    def expired(self) -> bool:
        return self.remaining() <= 0.0


def raise_if_cancelled(cancel_event: threading.Event | None) -> None:
    if cancel_event is not None and cancel_event.is_set():
        raise FetchCancelled


def interruptible_sleep(
    seconds: float,
    cancel_event: threading.Event | None,
    clock: Clock = SYSTEM_CLOCK,
) -> None:
    """Sleep ``seconds``, waking early with :class:`FetchCancelled` on cancel."""
    if seconds <= 0:
        return
    if cancel_event is None:
        clock.sleep(seconds)
    elif cancel_event.wait(seconds):
        raise FetchCancelled
