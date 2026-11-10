"""The background thread that crawls queued lead websites and copies the results to Postgres."""

import threading
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor

from app.helpers.http_fetcher import SITE_TIMEOUT, WebPageFetcher
from app.helpers.http_fetcher.urls import hostname_of
from app.services.google_map_scraper.scrape.scrape_types import (
    WebsiteBatch,
    WebsiteTarget,
)
from app.utils.errors import describe
from app.utils.logger import get_logger

from .findings import Finding
from .lead_fields import lead_columns, social_links
from .site.crawl import CrawlOptions, FetchFn, WebsiteCrawl
from .site.scope import origin_of, registrable_domain
from .storage.buffer import (
    CrawlBuffer,
    WebsiteSnapshot,
)
from .storage.crawl_store import CrawlStore
from .storage.postgres_sink import PostgresSink

logger = get_logger(__name__)

WEBSITE_WORKERS = 10
BATCHES_IN_PARALLEL = 3
_IDLE_POLL_SECONDS = 2
_RETRY_PAUSE_SECONDS = 2


def _origin_key(website: str) -> str:
    try:
        return origin_of(website)
    except ValueError:
        return website


class CrawlerWorker(threading.Thread):
    """Checks queued websites batch by batch until shut down."""

    def __init__(
        self,
        store: CrawlStore,
        fetch: FetchFn | None = None,
        sink: PostgresSink | None = None,
    ):
        """Create the worker thread; call :meth:`start` to begin."""
        super().__init__(daemon=True, name="crawler")
        self._store = store
        self._buffer = CrawlBuffer(store)
        fetcher = WebPageFetcher()
        self._fetch = fetch or (
            lambda url: fetcher.fetch_result(url, timeout=SITE_TIMEOUT)
        )
        self._sink = sink if sink is not None else PostgresSink.from_environment()
        self._stopped = threading.Event()

    def run(self) -> None:
        """Thread body: copy leftovers, then check websites until :meth:`shutdown` is called."""
        try:
            self._buffer.mark_interrupted()
        except Exception:
            logger.exception(
                "marking the crawls of an earlier run as interrupted failed"
            )
        try:
            self._store.requeue_running_websites()
        except Exception:
            logger.exception("requeuing the websites of an earlier run failed")
        try:
            self._copy_leftovers()
        except Exception:
            logger.exception("copying the finished websites of an earlier run failed")
        with ThreadPoolExecutor(BATCHES_IN_PARALLEL) as pool:
            for _ in range(BATCHES_IN_PARALLEL):
                pool.submit(self._check_batches)

    def _check_batches(self) -> None:
        while not self._stopped.is_set():
            try:
                batch = self._store.next_website_batch(WEBSITE_WORKERS)
                if batch is None:
                    self._stopped.wait(_IDLE_POLL_SECONDS)
                    continue
                self._check_batch(batch)
            except Exception:
                logger.exception("crawler failed; retrying")
                self._stopped.wait(_RETRY_PAUSE_SECONDS)

    def shutdown(self, wait: float = 0) -> None:
        """Stop after the current batches; optionally wait for the thread to end."""
        self._stopped.set()
        if wait and self.is_alive():
            self.join(wait)

    def _copy_leftovers(self) -> None:
        if not self._sink.enabled:
            return
        website_ids = self._buffer.finished_website_ids()
        if website_ids:
            logger.info(
                "copying %s finished website(s) left by an earlier run",
                len(website_ids),
            )
        for website_id in website_ids:
            try:
                snapshot = self._buffer.read_website(website_id)
            except Exception as error:
                logger.error(
                    "website %s could not be read from the buffer: %s",
                    website_id,
                    describe(error),
                    exc_info=error,
                )
                continue
            self._copy(website_id, snapshot)

    def _copy(self, website_id: str, snapshot: WebsiteSnapshot) -> None:
        if not self._sink.enabled:
            return
        try:
            self._sink.copy_website(snapshot)
        except Exception as error:  # noqa: BLE001 -- the rows stay in the buffer for the next start
            logger.warning(
                "website %s was not copied to Postgres and stays in the buffer: %s",
                website_id,
                describe(error),
            )
            return
        try:
            self._buffer.release_website(website_id)
        except Exception as error:
            logger.error(
                "website %s was copied to Postgres but could not be removed from the buffer: %s",
                website_id,
                describe(error),
                exc_info=error,
            )

    def _check_batch(self, batch: WebsiteBatch) -> None:
        groups: dict[str, list[WebsiteTarget]] = defaultdict(list)
        for target in batch.websites:
            groups[_origin_key(target.website)].append(target)
        with ThreadPoolExecutor(WEBSITE_WORKERS) as pool:
            checks = [
                pool.submit(self._check_origin_group, batch, targets)
                for targets in groups.values()
            ]
            for check in checks:
                check.result()

    def _check_origin_group(
        self, batch: WebsiteBatch, targets: list[WebsiteTarget]
    ) -> None:
        first, *others = targets
        fields, socials = self._check(batch, first)
        for target in others:
            self._save_fields(batch, target, fields, socials)

    def _save_fields(
        self,
        batch: WebsiteBatch,
        target: WebsiteTarget,
        fields: dict[str, str],
        socials: list[tuple[str, str]],
    ) -> None:
        try:
            self._store.save_website_result(
                batch.scrape_id, target.position, fields, socials
            )
        except Exception as error:  # noqa: BLE001 -- one lead must not skip the others
            logger.warning(
                "lead %s could not be saved: %s", target.row_id, describe(error)
            )

    def _check(
        self, batch: WebsiteBatch, target: WebsiteTarget
    ) -> tuple[dict[str, str], list[tuple[str, str]]]:
        crawl: WebsiteCrawl | None = None
        website_id: str | None = None
        try:
            origin = origin_of(target.website)
            host = hostname_of(origin)
            website_id = self._buffer.create_website(
                scrape_id=batch.scrape_id,
                lead_position=target.position,
                scrape_lead_id=target.row_id,
                host=host,
                registrable_domain=registrable_domain(host),
                origin=origin,
            )
            crawl = WebsiteCrawl(
                buffer=self._buffer,
                fetch=self._fetch,
                website_id=website_id,
                scrape_id=batch.scrape_id,
                lead_position=target.position,
                website=target.website,
                options=CrawlOptions(
                    max_pages=batch.crawl_max_pages,
                    max_depth=batch.crawl_max_depth,
                    store_html=batch.crawl_store_html,
                    collect_emails=batch.collect_emails,
                    collect_socials=batch.collect_socials,
                ),
            )
            try:
                crawl.run()
            except Exception as error:
                logger.exception("crawl of website %s raised", target.website)
                self._mark_failed(crawl.run_id, website_id, "crawl_error", error)
            else:
                if crawl.run_id is None:
                    self._buffer.fail_website(
                        website_id,
                        "run_not_started",
                        "the crawl run could not be created",
                    )
            snapshot = self._buffer.read_website(website_id)
            findings = [
                Finding(
                    group=finding["group"],
                    key=finding["key"],
                    value=finding["value"],
                    extra=finding["extra"],
                )
                for finding in snapshot.findings
            ]
            fields = lead_columns(findings, collect_emails=batch.collect_emails)
            socials = social_links(findings, collect_socials=batch.collect_socials)
            self._store.save_website_result(
                batch.scrape_id, target.position, fields, socials
            )
            self._copy(website_id, snapshot)
            return fields, socials
        except Exception as error:
            logger.exception("website %s could not be checked", target.website)
            if website_id is not None:
                self._mark_failed(
                    crawl.run_id if crawl is not None else None,
                    website_id,
                    "crawler_error",
                    error,
                )
            self._save_fields(batch, target, {}, [])
            return {}, []

    def _mark_failed(
        self, run_id: str | None, website_id: str, error_code: str, error: Exception
    ) -> None:
        if run_id is not None:
            try:
                self._buffer.finish_run(
                    run_id,
                    status="failed",
                    progress={},
                    error_code=error_code,
                    error_message=describe(error),
                )
            except Exception as mark_error:
                logger.error(
                    "crawl run %s could not be marked failed: %s",
                    run_id,
                    describe(mark_error),
                    exc_info=mark_error,
                )
        try:
            self._buffer.fail_website(website_id, error_code, describe(error))
        except Exception as mark_error:
            logger.error(
                "website %s could not be marked failed: %s",
                website_id,
                describe(mark_error),
                exc_info=mark_error,
            )
