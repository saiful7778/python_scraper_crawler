from app.helpers.http_fetcher import FetchResult
from app.services.google_map_scraper.scrape.lead_crawlability import LeadCrawlability


def _result(url: str, status: int = 200) -> FetchResult:
    return FetchResult(
        url=url,
        final_url=url,
        status=status,
        headers={"Content-Type": "text/html"},
        body=b"<html></html>",
        redirect_chain=(),
    )


def test_marks_each_origin_once_and_sets_the_row_field():
    calls: list[str] = []

    def fetch(url: str) -> FetchResult:
        calls.append(url)
        return _result(url, status=404 if "dead" in url else 200)

    rows = [
        {"website": "https://a.com/about"},
        {"website": "https://a.com/contact"},
        {"website": "https://dead.com/"},
        {"website": ""},
    ]

    LeadCrawlability(fetch)(rows)

    assert rows[0]["is_crawlable"] is True
    assert rows[1]["is_crawlable"] is True
    assert rows[2]["is_crawlable"] is False
    assert "is_crawlable" not in rows[3]
    assert calls == ["https://a.com", "https://dead.com"]


def test_an_unusable_website_is_not_crawlable():
    rows = [{"website": "ftp://a.com"}]

    LeadCrawlability(lambda url: _result(url))(rows)

    assert rows[0]["is_crawlable"] is False


def test_remembers_an_origin_across_batches():
    calls: list[str] = []

    def fetch(url: str) -> FetchResult:
        calls.append(url)
        return _result(url)

    checks = LeadCrawlability(fetch)

    checks([{"website": "https://a.com/one"}])
    checks([{"website": "https://a.com/two"}])

    assert calls == ["https://a.com"]
