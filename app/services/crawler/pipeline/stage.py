"""Claims queued websites from the FIFO queue and crawls them on threads."""

import threading
import time

from app.helpers.http_fetcher import SITE_TIMEOUT, WebPageFetcher
from app.services.buffer.crawl_buffer import RedisCrawlBuffer
from app.services.buffer.crawl_queue import CrawlJob, CrawlQueue
from app.services.crawler.site.crawl import CrawlOptions, FetchFn
from app.services.crawler.worker import check_website
from app.utils.errors import describe
from app.utils.logger import get_logger

logger = get_logger(__name__)

_ORIGIN_POLL_SECONDS = 0.2

CRAWL_OPTIONS = CrawlOptions(
    max_pages=100, max_depth=3, collect_emails=True, collect_socials=True
)


def site_fetch(fetcher: WebPageFetcher) -> FetchFn:
    """The fetch of a website, with the per-request time limit of the crawl."""
    return lambda url: fetcher.fetch_result(url, timeout=SITE_TIMEOUT)


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


class CrawlStage:
    """Threads that claim queued websites and crawl them until the queue is empty.

    A claimed job whose thread stops before ``complete`` stays in the queue's
    processing list, where ``requeue_processing`` finds it on the next start.
    """

    def __init__(
        self,
        queue: CrawlQueue,
        buffer: RedisCrawlBuffer,
        fetch: FetchFn,
        size: int,
        options: CrawlOptions,
    ):
        self._queue = queue
        self._buffer = buffer
        self._fetch = fetch
        self._size = size
        self._options = options
        self._origins = _Origins()
        self._stopped = threading.Event()
        self._threads: list[threading.Thread] = []

    def run_until_empty(self) -> None:
        """Crawl queued websites on ``size`` threads and return once none are left to claim."""
        self._threads = [
            threading.Thread(target=self._run, daemon=True, name=f"crawl-{number}")
            for number in range(self._size)
        ]
        logger.info("starting %d crawl thread(s)", self._size)
        for thread in self._threads:
            thread.start()
        for thread in self._threads:
            thread.join()

    def shutdown(self, wait: float) -> None:
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
                job = self._queue.claim()
            except Exception as error:
                logger.error(
                    "claiming a website failed: %s", describe(error), exc_info=error
                )
                return
            if job is None:
                logger.debug("%s: no websites left to claim", threading.current_thread().name)
                return
            self._handle(job)

    def _handle(self, job: CrawlJob) -> None:
        if not self._origins.acquire(job["origin"], self._stopped):
            logger.info("website %s not checked: the crawl is stopping", job["website"])
            return
        logger.info("crawl of %s: checking %s", job["website"], job["origin"])
        try:
            try:
                check_website(self._buffer, self._fetch, job, self._options)
            except Exception as error:
                logger.error(
                    "website %s could not be checked: %s",
                    job["website"],
                    describe(error),
                    exc_info=error,
                )
            try:
                self._queue.complete(job)
            except Exception as error:
                logger.error(
                    "website %s could not be completed in the queue: %s",
                    job["website"],
                    describe(error),
                    exc_info=error,
                )
        finally:
            self._origins.release(job["origin"])
