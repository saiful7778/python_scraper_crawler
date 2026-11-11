from dataclasses import dataclass

from app.helpers.http_fetcher import FetchResult
from app.services.crawler.site.crawl import FetchFn


@dataclass(frozen=True)
class CrawlCheck:
    crawlable: bool
    reason: str


def is_crawlable(http_status: int | None, content_type: str | None) -> bool:
    if http_status is None or not 200 <= http_status < 300:
        return False
    if content_type is None:
        return False
    media_type = content_type.split(";", 1)[0].strip().lower()
    return media_type == "text/html"


def check_origin(origin: str, fetch: FetchFn) -> CrawlCheck:
    try:
        result = fetch(origin)
    except Exception:
        return CrawlCheck(crawlable=False, reason="fetch_error")

    if result.error is not None or result.status == 0:
        return CrawlCheck(crawlable=False, reason="fetch_error")
    content_type = _header(result, "content-type")
    if not is_crawlable(result.status, content_type):
        if not 200 <= result.status < 300:
            return CrawlCheck(crawlable=False, reason=f"http_{result.status}")
        return CrawlCheck(crawlable=False, reason="not_html")
    return CrawlCheck(crawlable=True, reason="ok")


def _header(result: FetchResult, name: str) -> str | None:
    wanted = name.lower()
    for key, value in result.headers.items():
        if key.lower() == wanted:
            return value
    return None
