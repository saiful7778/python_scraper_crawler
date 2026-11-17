"""Runs finished-scrape work off the thread that feeds the scrape slots.

A tile's rows are saved to PostgreSQL after its scrape finishes. Doing that on the
thread that hands tiles to the proxy slots would keep every other slot idle until
the save ends. This pool takes the saves off that thread, so a slot can start its
next tile immediately.
"""

import queue
import threading
from collections.abc import Callable
from typing import TypeVar

T = TypeVar("T")


class FinishPool:
    """Runs ``work(scrape_id)`` on ``workers`` threads, in the order it was deferred."""

    def __init__(
        self,
        work: Callable[[str], T],
        workers: int = 1,
        name: str = "finish",
    ):
        if workers < 1:
            raise ValueError("a finish pool needs at least one worker")
        self._work = work
        self._queue: queue.Queue[str | None] = queue.Queue()
        self._errors: list[BaseException] = []
        self._lock = threading.Lock()
        self._closed = False
        self._threads = [
            threading.Thread(target=self._serve, name=f"{name}-{index}", daemon=True)
            for index in range(workers)
        ]
        for thread in self._threads:
            thread.start()

    def defer(self, scrape_id: str) -> None:
        """Queue one finished scrape; returns without waiting for its save."""
        if self._closed:
            raise RuntimeError("the finish pool is closed")
        self._queue.put(scrape_id)

    def drain(self) -> None:
        """Wait until every deferred scrape has been worked, including ones added meanwhile."""
        self._queue.join()

    def error(self) -> BaseException | None:
        """The first failure a worker recorded, if any."""
        with self._lock:
            return self._errors[0] if self._errors else None

    def close(self) -> None:
        """Work what is queued, then stop the threads."""
        if self._closed:
            return
        self.drain()
        self._closed = True
        for _ in self._threads:
            self._queue.put(None)
        for thread in self._threads:
            thread.join(5)

    def _serve(self) -> None:
        while True:
            scrape_id = self._queue.get()
            try:
                if scrape_id is None:
                    return
                self._work(scrape_id)
            except Exception as error:  # noqa: BLE001 -- one bad save must not stop the pool
                with self._lock:
                    self._errors.append(error)
            finally:
                self._queue.task_done()
