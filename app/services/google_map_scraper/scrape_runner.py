import time
from typing import cast

from redis import Redis
from redis.exceptions import RedisError

from app.helpers.http_fetcher import SITE_TIMEOUT, WebPageFetcher
from app.repositories import GoogleMapProfileRepository, LeadRepository
from app.services.buffer.client import BufferError, connect
from app.services.buffer.crawl_buffer import RedisCrawlBuffer
from app.services.buffer.crawl_sink import crawl_columns, crawl_socials, store_crawl
from app.services.buffer.crawl_types import WebsiteSnapshot
from app.services.buffer.discard import discard_scrape
from app.services.buffer.flusher import Flusher
from app.services.buffer.row_buffer import RowBuffer
from app.services.buffer.scrape_state import ScrapeState
from app.services.buffer.website_queue import WebsiteQueue
from app.services.crawler.pool import CrawlerPool, default_worker_count, state_options
from app.services.crawler.worker import skip_website
from app.utils.errors import ScraperError, describe
from app.utils.logger import get_logger
from config.redis import RedisConfig
from config.tortoise import TortoiseConfig

from .scrape.engine_config import EngineConfig
from .scrape.scrape_database import ScrapeDatabase
from .scrape.scrape_limits import BUFFER_TTL_SECONDS, MAX_RESUMES
from .scrape.scrape_run import RunOutcome, origin_key
from .scrape.scrape_settings import ScrapeSettings
from .scrape.scrape_types import LeadRow, RunResult, SettingsData
from .scrape.scrape_worker import ScrapeWorker

logger = get_logger(__name__)

_POLL_SECONDS = 1
#: Seconds the crawler threads get to finish the website they are on.
_POOL_STOP_SECONDS = 10
#: Seconds the flusher gets for its last flush; a slow PostgreSQL may need long.
_FINAL_FLUSH_SECONDS = 60


class ScrapeDeletedError(ScraperError):
    """The scrape was deleted while it was running, so it has no result."""


class ScrapeWorkerDiedError(ScraperError):
    """The scrape worker thread stopped before the scrape reached an outcome."""


class ScrapeInFlightError(ScraperError):
    """Websites were still being checked when the crawler pool stopped; the scrape resumes."""


class ScrapeFlushError(ScraperError):
    """PostgreSQL did not take every buffered lead or crawl, so the scrape stays resumable."""


class _Session:
    """The threads and Redis buffers of one ``run_scrape`` call."""

    def __init__(
        self,
        config: EngineConfig,
        redis: Redis,
        postgres: TortoiseConfig,
        known_keys: frozenset[str],
    ):
        self._redis = redis
        self._state = ScrapeState(redis, BUFFER_TTL_SECONDS)
        self._rows = RowBuffer(redis, BUFFER_TTL_SECONDS)
        self._queue = WebsiteQueue(redis, BUFFER_TTL_SECONDS)
        self._finished: dict[str, tuple[RunOutcome, list[LeadRow]]] = {}
        self._deleted: set[str] = set()
        self._crawled: dict[
            tuple[str, str], tuple[dict[str, str], list[tuple[str, str]]]
        ] = {}
        self._database = ScrapeDatabase(postgres)
        self._crawl = RedisCrawlBuffer(redis, BUFFER_TTL_SECONDS)
        fetcher = WebPageFetcher()
        self._worker = ScrapeWorker(
            self._database,
            redis,
            headless=config.headless,
            known_keys=known_keys,
            on_finished=self._keep_finished,
        )
        self._flusher = Flusher(
            self._rows,
            self._state,
            save_row=GoogleMapProfileRepository(LeadRepository()).save_row,
            crawl_sink=self._store_crawl,
            config=postgres,
            crawl_buffer=self._crawl,
        )
        self._pool = CrawlerPool(
            self._queue,
            self._crawl,
            lambda url: fetcher.fetch_result(url, timeout=SITE_TIMEOUT),
            default_worker_count(),
            options_of=state_options(self._state),
        )

    def run(self, name: str, settings: ScrapeSettings) -> RunResult:
        carried = self._resume_interrupted()
        scrape_id = self._database.create(
            name, cast(SettingsData, settings.model_dump(by_alias=True, mode="json"))
        )
        self._flusher.start()
        self._pool.start()
        self._worker.start()
        for earlier in carried:
            try:
                self._wait_for_scrape(earlier)
                self._wait_for_websites(earlier, self._max_seconds_of(earlier))
            except ScrapeDeletedError:
                logger.info("scrape %s was deleted while it ran", earlier)
                self._drop(earlier)
        try:
            self._wait_for_scrape(scrape_id)
            self._wait_for_websites(scrape_id, settings.max_seconds)
        except ScrapeDeletedError:
            self._drop(scrape_id)
            raise
        return self._finish(carried, scrape_id)

    def stop(self) -> None:
        for name, stop in (
            ("scrape worker", lambda: self._worker.shutdown(wait=1)),
            ("crawler pool", lambda: self._pool.shutdown(wait=1)),
            ("flusher", lambda: self._flusher.shutdown(wait=_FINAL_FLUSH_SECONDS)),
            ("scrape database", self._database.close),
        ):
            try:
                stop()
            except Exception:
                logger.exception("stopping the %s failed", name)
        for scrape_id in self._deleted:
            try:
                discard_scrape(self._redis, scrape_id)
            except Exception:
                logger.exception("discarding the deleted scrape %s failed", scrape_id)

    def _resume_interrupted(self) -> list[str]:
        resumed, failed = self._database.requeue_interrupted(MAX_RESUMES)
        if resumed or failed:
            logger.warning(
                "%s interrupted scrape(s) resume, %s failed after %s resumes",
                resumed,
                failed,
                MAX_RESUMES,
            )
        carried = self._database.pending_ids()
        for scrape_id in carried:
            self._queue.requeue_processing(scrape_id)
        return carried

    def _max_seconds_of(self, scrape_id: str) -> float:
        record = self._database.get(scrape_id)
        if record is None:
            raise ScrapeDeletedError(f"scrape {scrape_id} was deleted while it ran")
        return ScrapeSettings.model_validate(record["Data"]).max_seconds

    def _keep_finished(
        self, scrape_id: str, outcome: RunOutcome, rows: list[LeadRow]
    ) -> None:
        self._finished[scrape_id] = (outcome, rows)

    async def _store_crawl(self, snapshot: WebsiteSnapshot) -> None:
        await store_crawl(snapshot)
        key = (str(snapshot.website["scrape_id"]), str(snapshot.website["origin"]))
        self._crawled[key] = (crawl_columns(snapshot), crawl_socials(snapshot))

    def _check_exists(self, scrape_id: str) -> None:
        if self._database.get(scrape_id) is None:
            self._deleted.add(scrape_id)
            self._worker.cancel(scrape_id)
            raise ScrapeDeletedError(f"scrape {scrape_id} was deleted while it ran")

    def _wait_for_scrape(self, scrape_id: str) -> None:
        while scrape_id not in self._finished:
            self._check_exists(scrape_id)
            if not self._worker.is_alive():
                if self._worker.failure is not None:
                    raise self._worker.failure
                raise ScrapeWorkerDiedError(
                    f"the scrape worker stopped while scrape {scrape_id} had no outcome"
                )
            time.sleep(_POLL_SECONDS)

    def _wait_for_websites(self, scrape_id: str, seconds: float) -> None:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline and self._queue.pending(scrape_id):
            self._check_exists(scrape_id)
            time.sleep(_POLL_SECONDS)

    def _drop(self, scrape_id: str) -> None:
        discard_scrape(self._redis, scrape_id)
        self._finished.pop(scrape_id, None)

    def _finish(self, carried: list[str], scrape_id: str) -> RunResult:
        ids = [i for i in (*carried, scrape_id) if i in self._finished]
        self._worker.shutdown(wait=1)
        self._pool.shutdown(wait=_POOL_STOP_SECONDS)
        in_flight = [
            job
            for finished_id in ids
            for job in self._queue.processing_jobs(finished_id)
        ]
        if in_flight:
            self._flusher.shutdown(wait=_FINAL_FLUSH_SECONDS)
            names = ", ".join(job["website"] for job in in_flight)
            raise ScrapeInFlightError(
                f"{len(in_flight)} website(s) were still being checked when the crawler "
                f"pool stopped: {names}; the scrape stays resumable and checks them again "
                "on the next start"
            )
        for finished_id in ids:
            self._skip_unchecked(finished_id)
        self._flusher.shutdown(wait=_FINAL_FLUSH_SECONDS)
        for finished_id in ids:
            left = self._rows.length(finished_id)
            if left:
                raise ScrapeFlushError(
                    f"PostgreSQL did not take {left} buffered lead(s) of scrape "
                    f"{finished_id}; they stay in Redis and the scrape resumes on the next start"
                )
            crawls = self._crawl.finished_count(finished_id)
            if crawls:
                raise ScrapeFlushError(
                    f"PostgreSQL did not take {crawls} crawled website(s) of scrape "
                    f"{finished_id}; they stay in Redis and the scrape resumes on the next start"
                )
        result: RunResult | None = None
        for finished_id in ids:
            outcome, rows = self._finished[finished_id]
            self._add_crawl_results(finished_id, rows)
            progress = self._state.get_progress(finished_id)
            discard_scrape(self._redis, finished_id)
            if not self._database.finish(
                finished_id, outcome.status, outcome.note, progress
            ):
                if finished_id == scrape_id:
                    raise ScrapeDeletedError(
                        f"scrape {scrape_id} was deleted while it ran"
                    )
                continue
            if finished_id == scrape_id:
                result = RunResult(
                    scrape_id=scrape_id,
                    status=outcome.status,
                    note=outcome.note,
                    rows=rows,
                    blocked=bool(progress.get("blocked")),
                )
        if result is None:
            raise ScrapeWorkerDiedError(f"scrape {scrape_id} ended without an outcome")
        return result

    def _skip_unchecked(self, scrape_id: str) -> None:
        """Record the websites the time limit left in the queue as skipped."""
        jobs = self._queue.queued_jobs(scrape_id)
        if jobs:
            logger.warning(
                "scrape %s: %s website(s) not checked: ran out of time",
                scrape_id,
                len(jobs),
            )
        for job in jobs:
            try:
                skip_website(self._crawl, job)
            except ValueError as error:
                logger.warning(
                    "website %s of scrape %s has no crawlable address and is not recorded: %s",
                    job["website"],
                    scrape_id,
                    describe(error),
                )

    def _add_crawl_results(self, scrape_id: str, rows: list[LeadRow]) -> None:
        """Copy what the crawler found onto the in-memory leads of the same website."""
        for row in rows:
            if not row["website"]:
                continue
            found = self._crawled.get((scrape_id, origin_key(row["website"])))
            if found is None:
                continue
            columns, socials = found
            values: dict[str, str] = dict(columns)
            by_platform: dict[str, list[str]] = {}
            for platform, url in socials:
                by_platform.setdefault(platform, []).append(url)
            values.update(
                {platform: ", ".join(urls) for platform, urls in by_platform.items()}
            )
            row.update(cast(LeadRow, values))


def run_scrape(
    name: str,
    settings: ScrapeSettings,
    *,
    config: EngineConfig | None = None,
    known_keys: frozenset[str] = frozenset(),
    redis: Redis | None = None,
    postgres: TortoiseConfig | None = None,
) -> RunResult:
    """Store the scrape, run it on Redis and PostgreSQL and wait until it has an outcome.

    Scrapes an earlier run left unfinished resume first. Then the scrape worker
    runs this scrape while the flusher saves its leads and the crawler pool
    checks their websites. Once the worker is done, the websites get up to the
    scrape's ``max_seconds`` more; the rest are skipped. Leads whose keys are
    in ``known_keys`` are skipped. The Redis data of the scrape is deleted when
    it ends, and its ``scrapes`` row stays with the final status.

    :raises ScrapeDeletedError: If the scrape is deleted while it runs.
    :raises ScrapeWorkerDiedError: If the worker thread stops before the scrape
        has an outcome, so the wait would otherwise never end.
    :raises ScrapeInFlightError: If a website was still being checked when the
        crawler pool stopped; the scrape resumes on the next start.
    :raises ScrapeFlushError: If PostgreSQL did not take every buffered lead or
        crawl; they stay in Redis and the scrape resumes on the next start.
    :raises BufferError: If Redis fails.
    :raises ScrapeDatabaseError: If PostgreSQL fails.
    """
    session = _Session(
        config or EngineConfig(),
        redis or connect(RedisConfig()),
        postgres or TortoiseConfig(),
        known_keys,
    )
    try:
        return session.run(name, settings)
    except RedisError as error:
        raise BufferError(
            f"Redis failed while running the scrape: {describe(error)}"
        ) from error
    finally:
        session.stop()
