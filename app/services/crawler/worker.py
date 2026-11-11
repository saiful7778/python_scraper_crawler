"""Checks one website into the crawl buffer, or marks it skipped."""

from app.helpers.http_fetcher.urls import hostname_of
from app.services.buffer.crawl_buffer import RedisCrawlBuffer
from app.services.buffer.crawl_queue import CrawlJob
from app.utils.errors import describe
from app.utils.logger import get_logger

from .site.crawl import CrawlOptions, FetchFn, WebsiteCrawl
from .site.scope import origin_of, registrable_domain

logger = get_logger(__name__)


def check_website(
    buffer: RedisCrawlBuffer,
    fetch: FetchFn,
    job: CrawlJob,
    options: CrawlOptions,
) -> None:
    """Crawl one website into the buffer; a failure is recorded on its website."""
    crawl: WebsiteCrawl | None = None
    website_id: str | None = None
    try:
        origin = origin_of(job["website"])
        host = hostname_of(origin)
        website_id = buffer.create_website(
            host=host,
            registrable_domain=registrable_domain(host),
            origin=origin,
        )
        crawl = WebsiteCrawl(
            buffer=buffer,
            fetch=fetch,
            website_id=website_id,
            website=job["website"],
            options=options,
        )
        try:
            crawl.run()
        except Exception as error:
            logger.exception("crawl of website %s raised", job["website"])
            _mark_failed(buffer, crawl.run_id, website_id, "crawl_error", error)
        else:
            if crawl.run_id is None:
                buffer.fail_website(
                    website_id,
                    "run_not_started",
                    "the crawl run could not be created",
                )
    except Exception as error:
        logger.exception("website %s could not be checked", job["website"])
        if website_id is not None:
            _mark_failed(
                buffer,
                crawl.run_id if crawl is not None else None,
                website_id,
                "crawler_error",
                error,
            )


def _mark_failed(
    buffer: RedisCrawlBuffer,
    run_id: str | None,
    website_id: str,
    error_code: str,
    error: Exception,
) -> None:
    if run_id is not None:
        try:
            buffer.finish_run(
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
        buffer.fail_website(website_id, error_code, describe(error))
    except Exception as mark_error:
        logger.error(
            "website %s could not be marked failed: %s",
            website_id,
            describe(mark_error),
            exc_info=mark_error,
        )
