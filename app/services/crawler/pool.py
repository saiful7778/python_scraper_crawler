"""A pool of threads that claim queued websites from Redis and check them."""

import os
import threading
import time
from collections.abc import Callable

from app.services.buffer.crawl_buffer import RedisCrawlBuffer
from app.services.buffer.scrape_state import ScrapeState
from app.services.buffer.website_queue import WebsiteJob, WebsiteQueue
from app.utils.errors import describe
from app.utils.logger import get_logger
from config.crawler import CrawlerConfig

from .site.crawl import CrawlOptions, FetchFn
from .worker import check_website

logger = get_logger(__name__)

_IDLE_POLL_SECONDS = 1
_ORIGIN_POLL_SECONDS = 0.2

OptionsFn = Callable[[str], CrawlOptions]


def worker_count(cpu: int | None, maximum: int) -> int:
    """The number of crawler threads: the CPU count, capped by ``maximum``."""
    if maximum < 1:
        raise ValueError(
            f"the maximum number of crawler workers must be at least 1, not {maximum}"
        )
    return min(cpu or 1, maximum)


def default_worker_count() -> int:
    """The pool size for this machine, from ``CRAWLER_MAX_WORKERS``."""
    return worker_count(os.cpu_count(), CrawlerConfig().max_workers)


def _default_options(scrape_id: str) -> CrawlOptions:
    return CrawlOptions(
        max_pages=100, max_depth=3, collect_emails=False, collect_socials=False
    )


def state_options(state: ScrapeState) -> OptionsFn:
    """The crawl options of a scrape, read from the settings kept in Redis."""

    def options_of(scrape_id: str) -> CrawlOptions:
        defaults = _default_options(scrape_id)
        settings = state.get_settings(scrape_id) or {}
        return CrawlOptions(
            max_pages=settings.get("crawl_max_pages", defaults.max_pages),
            max_depth=settings.get("crawl_max_depth", defaults.max_depth),
            collect_emails=settings.get("email", defaults.collect_emails),
            collect_socials=settings.get("socials", defaults.collect_socials),
        )

    return options_of


class _Origins:
    """The origins being checked now, so no two threads check one at the same time."""

    def __init__(self) -> None:
        self._busy: set[str] = set()
        self._changed = threading.Condition()

    def acquire(self, origin: str, stopped: threading.Event) -> bool:
        """Wait until ``origin`` is free and take it; False when stopped first."""
        with self._changed:
            while origin in self._busy:
                if stopped.is_set():
                    return False
                self._changed.wait(_ORIGIN_POLL_SECONDS)
            self._busy.add(origin)
            return True

    def release(self, origin: str) -> None:
        with self._changed:
            self._busy.discard(origin)
            self._changed.notify_all()


class CrawlerPool:
    """Threads that claim websites from the queue of any scrape and crawl them.

    A claimed job whose thread stops before ``complete`` stays in the queue's
    processing list, where ``requeue_processing`` finds it on resume.
    """

    def __init__(
        self,
        queue: WebsiteQueue,
        buffer: RedisCrawlBuffer,
        fetch: FetchFn,
        size: int,
        options_of: OptionsFn = _default_options,
    ):
        self._queue = queue
        self._buffer = buffer
        self._fetch = fetch
        self._size = size
        self._options_of = options_of
        self._origins = _Origins()
        self._stopped = threading.Event()
        self._threads: list[threading.Thread] = []

    def start(self) -> None:
        """Start the threads; starting a started pool does nothing."""
        if self._threads:
            return
        for number in range(self._size):
            thread = threading.Thread(
                target=self._run, daemon=True, name=f"crawler-{number}"
            )
            thread.start()
            self._threads.append(thread)

    def shutdown(self, wait: float = 0) -> None:
        """Tell the threads to stop after their current website; wait up to ``wait`` seconds in all."""
        self._stopped.set()
        deadline = time.monotonic() + wait
        for thread in self._threads:
            left = deadline - time.monotonic()
            if left > 0 and thread.is_alive():
                thread.join(left)

    def _run(self) -> None:
        while not self._stopped.is_set():
            try:
                job = self._claim()
            except Exception as error:
                logger.error(
                    "claiming a website failed: %s", describe(error), exc_info=error
                )
                self._stopped.wait(_IDLE_POLL_SECONDS)
                continue
            if job is None:
                self._stopped.wait(_IDLE_POLL_SECONDS)
                continue
            self._handle(job)

    def _claim(self) -> WebsiteJob | None:
        for scrape_id in self._queue.scrape_ids_with_work():
            job = self._queue.claim(scrape_id)
            if job is not None:
                return job
        return None

    def _handle(self, job: WebsiteJob) -> None:
        if not self._origins.acquire(job["origin"], self._stopped):
            return
        try:
            try:
                check_website(
                    self._buffer, self._fetch, job, self._options_of(job["scrape_id"])
                )
            except Exception as error:
                logger.error(
                    "website %s could not be checked: %s",
                    job["website"],
                    describe(error),
                    exc_info=error,
                )
            try:
                self._queue.complete(job["scrape_id"], job)
            except Exception as error:
                logger.error(
                    "website %s could not be completed in the queue: %s",
                    job["website"],
                    describe(error),
                    exc_info=error,
                )
        finally:
            self._origins.release(job["origin"])
