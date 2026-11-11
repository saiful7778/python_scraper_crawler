import threading
import time
from urllib.parse import urlsplit

import fakeredis
import pytest

from app.helpers.http_fetcher.models import FetchResult, build_result
from app.services.buffer.crawl_buffer import RedisCrawlBuffer
from app.services.buffer.scrape_state import ScrapeState
from app.services.buffer.website_queue import WebsiteJob, WebsiteQueue
from app.services.crawler import pool as pool_module
from app.services.crawler.pool import (
    CrawlerPool,
    default_worker_count,
    state_options,
    worker_count,
)
from app.services.crawler.site.crawl import CrawlOptions, WebsiteCrawl

TTL = 3600
PAGE = '<a href="mailto:hi@bakery.studio.de">mail</a>'
OPTIONS = CrawlOptions(
    max_pages=10, max_depth=2, collect_emails=True, collect_socials=False
)


@pytest.mark.parametrize(
    "cpu, maximum, expected",
    [(None, 8, 1), (1, 8, 1), (4, 8, 4), (32, 8, 8), (8, 8, 8)],
)
def test_worker_count_is_cpu_capped_by_maximum(cpu, maximum, expected):
    assert worker_count(cpu, maximum) == expected


@pytest.mark.parametrize("maximum", [0, -1])
def test_worker_count_rejects_a_maximum_below_one(maximum):
    with pytest.raises(ValueError):
        worker_count(4, maximum)


def test_default_worker_count_reads_the_configured_maximum(monkeypatch):
    monkeypatch.setattr(pool_module.os, "cpu_count", lambda: 16)
    monkeypatch.delenv("CRAWLER_MAX_WORKERS", raising=False)
    assert default_worker_count() == 8

    monkeypatch.setenv("CRAWLER_MAX_WORKERS", "3")
    assert default_worker_count() == 3


def _origin_of(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}"


class _Fetch:
    def __init__(self, page: str = PAGE, pause: float = 0):
        self._page = page
        self._pause = pause
        self._lock = threading.Lock()
        self._running: dict[str, int] = {}
        self.most_at_once: dict[str, int] = {}

    def __call__(self, url: str) -> FetchResult:
        origin = _origin_of(url)
        with self._lock:
            self._running[origin] = self._running.get(origin, 0) + 1
            self.most_at_once[origin] = max(
                self.most_at_once.get(origin, 0), self._running[origin]
            )
        try:
            time.sleep(self._pause)
            if urlsplit(url).path in ("", "/"):
                return build_result(
                    trail=[url],
                    status=200,
                    headers={"content-type": "text/html"},
                    body=self._page.encode(),
                )
            return build_result(trail=[url], status=404)
        finally:
            with self._lock:
                self._running[origin] -= 1


def _wait_until(condition, seconds: float = 10) -> None:
    deadline = time.monotonic() + seconds
    while not condition() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert condition()


@pytest.fixture
def redis():
    return fakeredis.FakeRedis(decode_responses=True)


@pytest.fixture
def queue(redis):
    return WebsiteQueue(redis, TTL)


@pytest.fixture
def buffer(redis):
    return RedisCrawlBuffer(redis, TTL)


@pytest.fixture
def make_pool(queue, buffer):
    pools: list[CrawlerPool] = []

    def make(fetch=None, size=2) -> CrawlerPool:
        pool = CrawlerPool(
            queue,
            buffer,
            fetch or _Fetch(),
            size,
            options_of=lambda scrape_id: OPTIONS,
        )
        pools.append(pool)
        return pool

    yield make
    for pool in pools:
        pool.shutdown(wait=2)


def _queue_website(redis, queue, scrape_id: str, position: int, website: str) -> None:
    ScrapeState(redis, TTL).create(scrape_id, {})
    queue.enqueue(
        scrape_id,
        WebsiteJob(
            scrape_id=scrape_id,
            position=position,
            row_id=f"lead-{scrape_id}-{position}",
            website=website,
            origin=website,
        ),
    )


def _drained(queue, *scrape_ids: str):
    return lambda: all(queue.pending(scrape_id) == 0 for scrape_id in scrape_ids)


def _websites(buffer) -> dict:
    snapshots = [buffer.read_website(i) for i in buffer.finished_website_ids()]
    return {snapshot.website["origin"]: snapshot for snapshot in snapshots}


def _pool_threads() -> list[threading.Thread]:
    return [t for t in threading.enumerate() if t.name.startswith("crawler-")]


def test_pool_crawls_queued_websites_into_the_buffer_and_completes_them(
    redis, queue, buffer, make_pool
):
    _queue_website(redis, queue, "scrape-1", 0, "https://bakery.studio.de")
    pool = make_pool()
    pool.start()
    _wait_until(_drained(queue, "scrape-1"))
    pool.shutdown(wait=2)

    snapshot = _websites(buffer)["https://bakery.studio.de"]
    assert snapshot.website["status"] == "done"
    assert [f["value"] for f in snapshot.findings if f["key"] == "email"] == [
        "hi@bakery.studio.de"
    ]
    assert queue.pending("scrape-1") == 0


def test_pool_takes_jobs_from_every_scrape_with_work(redis, queue, buffer, make_pool):
    _queue_website(redis, queue, "scrape-1", 0, "https://one.example")
    _queue_website(redis, queue, "scrape-2", 0, "https://two.example")
    pool = make_pool()
    pool.start()
    _wait_until(_drained(queue, "scrape-1", "scrape-2"))
    pool.shutdown(wait=2)

    assert set(_websites(buffer)) == {"https://one.example", "https://two.example"}


def test_pool_crawls_jobs_queued_after_it_started(redis, queue, buffer, make_pool):
    pool = make_pool()
    pool.start()
    _queue_website(redis, queue, "scrape-1", 0, "https://late.example")
    _wait_until(lambda: buffer.finished_website_ids())
    pool.shutdown(wait=2)

    assert set(_websites(buffer)) == {"https://late.example"}
    assert queue.pending("scrape-1") == 0


def test_pool_gives_each_crawl_the_options_of_its_scrape(redis, queue, buffer):
    _queue_website(redis, queue, "scrape-1", 0, "https://bakery.studio.de")
    seen: list[str] = []

    def options_of(scrape_id: str) -> CrawlOptions:
        seen.append(scrape_id)
        return CrawlOptions(
            max_pages=1, max_depth=0, collect_emails=True, collect_socials=False
        )

    pool = CrawlerPool(queue, buffer, _Fetch(), 1, options_of=options_of)
    pool.start()
    _wait_until(_drained(queue, "scrape-1"))
    pool.shutdown(wait=2)

    (snapshot,) = _websites(buffer).values()
    assert seen == ["scrape-1"]
    assert snapshot.runs[0]["config"]["max_pages"] == 1


def test_one_failing_website_does_not_stop_another(
    redis, queue, buffer, make_pool, monkeypatch
):
    original_run = WebsiteCrawl.run

    def run(self):
        if "bad.de" in self._website:
            raise RuntimeError("boom")
        return original_run(self)

    monkeypatch.setattr(WebsiteCrawl, "run", run)
    _queue_website(redis, queue, "scrape-1", 0, "https://bad.de")
    _queue_website(redis, queue, "scrape-1", 1, "https://good.de")
    pool = make_pool(size=1)
    pool.start()
    _wait_until(_drained(queue, "scrape-1"))
    pool.shutdown(wait=2)

    websites = _websites(buffer)
    assert websites["https://good.de"].website["status"] == "done"
    bad = websites["https://bad.de"].website
    assert (bad["status"], bad["error_code"]) == ("failed", "crawl_error")


def test_website_whose_run_never_starts_is_marked_failed_and_completed(
    redis, queue, buffer, make_pool, monkeypatch
):
    def create_run(self, **kwargs):
        raise RuntimeError("no run")

    monkeypatch.setattr(RedisCrawlBuffer, "create_run", create_run)
    _queue_website(redis, queue, "scrape-1", 0, "https://bakery.studio.de")
    pool = make_pool()
    pool.start()
    _wait_until(_drained(queue, "scrape-1"))
    pool.shutdown(wait=2)

    website = _websites(buffer)["https://bakery.studio.de"].website
    assert website["status"] == "failed"
    assert website["error_code"] == "run_not_started"


def test_a_check_that_cannot_start_is_completed_and_the_thread_survives(
    redis, queue, buffer, make_pool, monkeypatch
):
    original_create = RedisCrawlBuffer.create_website

    def create_website(self, **kwargs):
        if kwargs["lead_position"] == 0:
            raise RuntimeError("buffer is down")
        return original_create(self, **kwargs)

    monkeypatch.setattr(RedisCrawlBuffer, "create_website", create_website)
    _queue_website(redis, queue, "scrape-1", 0, "https://first.example")
    _queue_website(redis, queue, "scrape-1", 1, "https://second.example")
    pool = make_pool(size=1)
    pool.start()
    _wait_until(_drained(queue, "scrape-1"))
    pool.shutdown(wait=2)

    assert set(_websites(buffer)) == {"https://second.example"}


def test_the_same_origin_is_never_checked_by_two_threads_at_once(
    redis, queue, buffer, make_pool
):
    fetch = _Fetch(pause=0.05)
    _queue_website(redis, queue, "scrape-1", 0, "https://same.example")
    _queue_website(redis, queue, "scrape-2", 0, "https://same.example")
    pool = make_pool(fetch=fetch, size=4)
    pool.start()
    _wait_until(_drained(queue, "scrape-1", "scrape-2"))
    pool.shutdown(wait=2)

    assert fetch.most_at_once["https://same.example"] == 1
    assert len(buffer.finished_website_ids()) == 2


def test_different_origins_are_checked_at_the_same_time(
    redis, queue, buffer, make_pool
):
    both_started = threading.Barrier(2, timeout=5)
    first_call = {"https://one.example": True, "https://two.example": True}
    lock = threading.Lock()
    inner = _Fetch()

    def fetch(url: str) -> FetchResult:
        origin = _origin_of(url)
        with lock:
            first = first_call[origin]
            first_call[origin] = False
        if first:
            both_started.wait()
        return inner(url)

    _queue_website(redis, queue, "scrape-1", 0, "https://one.example")
    _queue_website(redis, queue, "scrape-1", 1, "https://two.example")
    pool = make_pool(fetch=fetch, size=2)
    pool.start()
    _wait_until(_drained(queue, "scrape-1"))
    pool.shutdown(wait=2)

    assert not both_started.broken
    assert len(buffer.finished_website_ids()) == 2


def test_shutdown_stops_every_thread(make_pool):
    pool = make_pool(size=3)
    pool.start()
    assert len(_pool_threads()) == 3

    pool.shutdown(wait=2)

    assert _pool_threads() == []


def test_start_twice_does_not_add_threads(make_pool):
    pool = make_pool(size=2)
    pool.start()
    pool.start()
    assert len(_pool_threads()) == 2


def test_shutdown_waits_one_deadline_for_all_threads_not_one_per_thread(
    redis, queue, make_pool
):
    release = threading.Event()
    started = threading.Barrier(3, timeout=5)

    def stuck(url):
        started.wait()
        release.wait(10)
        return build_result(trail=[url], status=404)

    for number in range(2):
        _queue_website(redis, queue, "scrape-1", number, f"https://s{number}.example")
    pool = make_pool(fetch=stuck, size=2)
    pool.start()
    started.wait()
    began = time.monotonic()
    pool.shutdown(wait=0.3)
    elapsed = time.monotonic() - began
    release.set()

    assert elapsed < 0.55


def test_state_options_read_the_settings_of_the_scrape(redis):
    state = ScrapeState(redis, TTL)
    state.create(
        "s1",
        {"email": True, "socials": True, "crawl_max_pages": 7, "crawl_max_depth": 2},
    )
    options_of = state_options(state)

    assert options_of("s1") == CrawlOptions(
        max_pages=7, max_depth=2, collect_emails=True, collect_socials=True
    )


def test_state_options_default_to_no_collection_when_the_scrape_is_gone(redis):
    options = state_options(ScrapeState(redis, TTL))("gone")
    assert (options.collect_emails, options.collect_socials) == (False, False)
