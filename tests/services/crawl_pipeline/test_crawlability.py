import pytest

from app.helpers.http_fetcher import FetchError, FetchResult
from app.services.crawl_pipeline.crawlability import CrawlCheck, check_origin, is_crawlable


def make_result(
    *,
    url="https://example.com/",
    final_url=None,
    status=200,
    headers=None,
    error=None,
):
    return FetchResult(
        url=url,
        final_url=final_url or url,
        status=status,
        headers={"Content-Type": "text/html; charset=utf-8"} if headers is None else headers,
        body=b"<html></html>",
        redirect_chain=(),
        error=error,
    )


@pytest.mark.parametrize(
    ("status", "content_type", "expected"),
    [
        (200, "text/html", True),
        (200, "text/html; charset=utf-8", True),
        (200, "TEXT/HTML; charset=utf-8", True),
        (404, "text/html", False),
        (200, "application/pdf", False),
        (200, None, False),
        (None, "text/html", False),
    ],
)
def test_is_crawlable(status, content_type, expected):
    assert is_crawlable(status, content_type) is expected


def test_origin_serving_html_is_crawlable():
    seen = []

    def fetch(url):
        seen.append(url)
        return make_result(url=url)

    check = check_origin("https://example.com", fetch)

    assert seen == ["https://example.com"]
    assert check == CrawlCheck(crawlable=True, reason="ok")


def test_redirect_to_2xx_html_page_is_crawlable():
    def fetch(url):
        return make_result(
            url=url,
            final_url="https://www.example.com/home",
            status=200,
            headers={"content-type": "text/html"},
        )

    check = check_origin("http://example.com", fetch)

    assert check == CrawlCheck(crawlable=True, reason="ok")


def test_not_found_is_not_crawlable():
    def fetch(url):
        return make_result(url=url, status=404)

    check = check_origin("https://example.com", fetch)

    assert check == CrawlCheck(crawlable=False, reason="http_404")


def test_pdf_is_not_crawlable():
    def fetch(url):
        return make_result(url=url, headers={"Content-Type": "application/pdf"})

    check = check_origin("https://example.com", fetch)

    assert check == CrawlCheck(crawlable=False, reason="not_html")


def test_fetch_that_raises_returns_fetch_error():
    def fetch(url):
        raise RuntimeError("connection reset")

    check = check_origin("https://example.com", fetch)

    assert check == CrawlCheck(crawlable=False, reason="fetch_error")


def test_fetch_result_with_error_returns_fetch_error():
    def fetch(url):
        return make_result(url=url, status=0, error=FetchError.TIMEOUT)

    check = check_origin("https://example.com", fetch)

    assert check == CrawlCheck(crawlable=False, reason="fetch_error")
