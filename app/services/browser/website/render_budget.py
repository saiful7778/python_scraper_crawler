"""A time budget shared by renders, and the event loop they run on."""

import concurrent.futures
import threading
import time
from collections.abc import Callable
from typing import Self

from .constants import DEFAULT_BUDGET_SECONDS, MIN_REMAINING_SECONDS, MIN_START_SECONDS
from .event_loop_thread import EventLoopThread
from .render_result import RenderResult, budget_exhausted
from .render_session import RenderSession


class RenderBudget:
    """A time limit for one or more renders, and the browser behind them.

    The deadline starts on the first :meth:`fetch`. That call also starts
    the event loop and a :class:`RenderSession`. Once :meth:`close` runs,
    further fetches return ``budget_exhausted``.

    :param seconds: Length of the budget, counted from the first fetch.
    """

    def __init__(self, seconds: float = DEFAULT_BUDGET_SECONDS) -> None:
        self.seconds = seconds
        self.deadline: float | None = None
        self.closed = False
        self._lock = threading.Lock()
        self._loop: EventLoopThread | None = None
        self._session: RenderSession | None = None

    def fetch(self, url: str, is_public_ip: Callable[[str], bool]) -> RenderResult:
        with self._lock:
            if self.closed:
                return budget_exhausted()
            if self.deadline is None:
                self.deadline = time.monotonic() + self.seconds
            minimum = MIN_START_SECONDS if self._loop is None else MIN_REMAINING_SECONDS
            if self.deadline - time.monotonic() < minimum:
                return budget_exhausted()
            if self._loop is None:
                self._loop = EventLoopThread()
                self._loop.start()
            future = self._loop.submit(self._render(url, is_public_ip))
        try:
            return future.result()
        except concurrent.futures.CancelledError:
            # close() ran while this page was still rendering.
            return budget_exhausted()

    async def _render(
        self, url: str, is_public_ip: Callable[[str], bool]
    ) -> RenderResult:
        if self._session is None:
            assert self.deadline is not None
            self._session = RenderSession(is_public_ip, self.deadline)
        return await self._session.render(url)

    def close(self) -> None:
        with self._lock:
            if self.closed:
                return
            self.closed = True
            loop = self._loop
            if loop is None:
                return
            future = loop.submit(self._close_session())
        try:
            future.result()
        finally:
            loop.stop()

    async def _close_session(self) -> None:
        # Runs on the loop, after any fetch scheduled before close() has created the session.
        if self._session is not None:
            await self._session.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()
