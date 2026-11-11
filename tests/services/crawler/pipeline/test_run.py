import pytest

from app.helpers.http_fetcher import FetchResult
from app.models import CrawlWebsiteModel
from app.services.buffer.crawl_queue import CrawlQueue
from app.services.crawler.pipeline.run import CrawlSummary, crawl_one
from app.services.crawler.pipeline.stage import CRAWL_OPTIONS
from tests.services.crawler.pipeline.conftest import in_database

HTML = b"<html><head><title>Home</title></head><body>Bakery</body></html>"
OPTS = CRAWL_OPTIONS


def page(url, status=200, body=HTML):
    return FetchResult(
        url=url,
        final_url=url,
        status=status,
        headers={"Content-Type": "text/html; charset=utf-8"},
        body=body if status == 200 else b"",
        redirect_chain=(),
        error=None,
    )


def html_fetch(url):
    return page(url)


def not_found_fetch(url):
    return page(url, status=404)


def stored_origins(config):
    return in_database(
        config, lambda: CrawlWebsiteModel.all().values_list("origin", flat=True)
    )


def test_crawl_one_without_finish_leaves_other_queued_jobs(fake_redis, postgres):
    CrawlQueue(fake_redis).enqueue(
        {"website": "https://other.de", "origin": "https://other.de"}
    )

    summary = crawl_one(
        "a.de",
        redis=fake_redis,
        postgres=postgres,
        options=OPTS,
        fetch=html_fetch,
    )

    assert summary.checked == 1
    assert [job["origin"] for job in CrawlQueue(fake_redis).queued_jobs()] == [
        "https://other.de"
    ]


def test_crawl_one_with_finish_drains_the_queue(fake_redis, postgres):
    CrawlQueue(fake_redis).enqueue(
        {"website": "https://other.de", "origin": "https://other.de"}
    )

    crawl_one(
        "a.de",
        redis=fake_redis,
        postgres=postgres,
        options=OPTS,
        finish_incomplete=True,
        fetch=html_fetch,
    )

    assert CrawlQueue(fake_redis).queued_jobs() == []


def test_crawl_one_records_a_404_as_rejected_without_a_queue_entry(
    fake_redis, postgres
):
    summary = crawl_one(
        "gone.de",
        redis=fake_redis,
        postgres=postgres,
        options=OPTS,
        fetch=not_found_fetch,
    )

    assert summary == CrawlSummary(queued=0, rejected=1, skipped=0, checked=1)
    assert CrawlQueue(fake_redis).queued_jobs() == []
    assert stored_origins(postgres) == ["https://gone.de"]


def test_crawl_one_accepts_a_website_without_scheme(fake_redis, postgres):
    summary = crawl_one(
        "a.de",
        redis=fake_redis,
        postgres=postgres,
        options=OPTS,
        fetch=html_fetch,
    )

    assert summary.queued == 0 and summary.rejected == 0
    assert stored_origins(postgres) == ["https://a.de"]


def test_crawl_one_twice_keeps_one_row(fake_redis, postgres):
    for _ in range(2):
        crawl_one(
            "a.de",
            redis=fake_redis,
            postgres=postgres,
            options=OPTS,
            fetch=html_fetch,
        )

    assert stored_origins(postgres) == ["https://a.de"]


def test_crawl_one_records_a_non_html_page_as_rejected(fake_redis, postgres):
    def pdf_fetch(url):
        return FetchResult(
            url=url,
            final_url=url,
            status=200,
            headers={"Content-Type": "application/pdf"},
            body=b"%PDF",
            redirect_chain=(),
            error=None,
        )

    summary = crawl_one(
        "docs.de",
        redis=fake_redis,
        postgres=postgres,
        options=OPTS,
        fetch=pdf_fetch,
    )

    assert summary == CrawlSummary(queued=0, rejected=1, skipped=0, checked=1)
    assert CrawlQueue(fake_redis).queued_jobs() == []
    assert stored_origins(postgres) == ["https://docs.de"]
