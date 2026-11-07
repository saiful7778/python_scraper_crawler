"""An asyncio event loop that runs forever on a daemon thread."""

import asyncio
import threading
from collections.abc import Coroutine
from concurrent.futures import Future
from typing import Any


class EventLoopThread:
    """Runs its own asyncio loop on a daemon thread.

    Coroutines are handed over with :meth:`submit`, which is safe to call
    from any thread. :meth:`stop` ends the loop and closes it.
    """

    def __init__(self) -> None:
        self._loop = asyncio.new_event_loop()
        self._loop.set_exception_handler(_handle_loop_exception)
        self._thread = threading.Thread(target=self._loop.run_forever, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def submit(self, coroutine: Coroutine[Any, Any, Any]) -> Future:
        """Schedule ``coroutine`` on the loop and return a thread-safe future."""
        return asyncio.run_coroutine_threadsafe(coroutine, self._loop)

    def stop(self) -> None:
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join()
        self._loop.close()


def _handle_loop_exception(loop: asyncio.AbstractEventLoop, context: dict) -> None:
    # A page closed under a pending Playwright call is expected during shutdown.
    if type(context.get("exception")).__name__ != "TargetClosedError":
        loop.default_exception_handler(context)
