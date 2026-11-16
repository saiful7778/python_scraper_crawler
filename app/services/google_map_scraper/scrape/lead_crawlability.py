"""Marks the websites of queued lead rows as crawlable or not."""

from concurrent.futures import ThreadPoolExecutor

from app.helpers.http_fetcher import WebPageFetcher
from app.services.crawler.pipeline.crawlability import check_origin
from app.services.crawler.pipeline.stage import site_fetch
from app.services.crawler.site.crawl import FetchFn
from app.services.crawler.site.scope import origin_of
from app.utils.errors import describe
from app.utils.logger import get_logger

from .scrape_types import LeadRow

logger = get_logger(__name__)

_WORKERS = 8


class LeadCrawlability:
    """Sets ``is_crawlable`` on lead rows, checking each origin once per scrape.

    The check runs when a finished tile's rows are saved, so a run that stops
    early keeps the result of every tile that finished.
    """

    def __init__(self, fetch: FetchFn | None = None) -> None:
        self._fetch = fetch or site_fetch(WebPageFetcher())
        self._checked: dict[str, bool] = {}

    def __call__(self, rows: list[LeadRow]) -> None:
        websites = list(
            dict.fromkeys(row.get("website", "") for row in rows if row.get("website"))
        )
        origins = self._origins(websites)
        missing = list(
            dict.fromkeys(
                origin
                for origin in origins.values()
                if origin not in self._checked
            )
        )
        for origin, crawlable in self._check(missing):
            self._checked[origin] = crawlable
        for row in rows:
            website = row.get("website", "")
            if not website:
                continue
            origin = origins.get(website)
            row["is_crawlable"] = (
                self._checked[origin] if origin is not None else False
            )

    def _origins(self, websites: list[str]) -> dict[str, str]:
        origins: dict[str, str] = {}
        for website in websites:
            try:
                origins[website] = origin_of(website)
            except ValueError as error:
                logger.warning(
                    "website %r has no usable origin: %s", website, describe(error)
                )
        return origins

    def _check(self, origins: list[str]) -> list[tuple[str, bool]]:
        def check(origin: str) -> tuple[str, bool]:
            result = check_origin(origin, self._fetch)
            if not result.crawlable:
                logger.info("website %s is not crawlable: %s", origin, result.reason)
            return origin, result.crawlable

        with ThreadPoolExecutor(max_workers=_WORKERS) as pool:
            return list(pool.map(check, origins))
