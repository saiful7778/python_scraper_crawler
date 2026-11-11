"""Crawls one website, step by step, recording everything in the crawl buffer.

Every step runs in its own ``try``: a failing step is logged as an error event
and marks the run ``partial``, and the remaining steps still run.
"""

import hashlib
import time
from collections import deque
from collections.abc import Callable
from dataclasses import asdict, dataclass

from app.helpers.http_fetcher.errors import FetchError
from app.helpers.http_fetcher.models import FetchResult
from app.helpers.http_fetcher.urls import hostname_of
from app.services.buffer.crawl_buffer import RedisCrawlBuffer
from app.services.buffer.crawl_types import LinkSeed, PageResult, PageSeed
from app.utils.errors import describe
from app.utils.logger import get_logger

from ..document import Document, absolute
from ..extractors.business import find_business
from ..extractors.contact import find_contact
from ..extractors.seo import find_seo
from ..extractors.social import find_social
from ..extractors.tech import find_tech
from ..findings import Finding, parse_page
from .checks import find_site_checks
from .robots import RobotTxtPolicy
from .scope import (
    is_in_scope,
    normalize,
    origin_of,
    registrable_domain,
)
from .sitemap import sitemap_urls

logger = get_logger(__name__)

MAX_BODY_BYTES = 2_000_000
MAX_SITEMAPS = 5
HTML_TYPES = ("text/html", "application/xhtml+xml", "text/plain")
QUIET_SKIPS = frozenset({"robots_disallowed", "out_of_scope", "not_html", "max_depth"})
_TEXT_LIMIT = 500

FetchFn = Callable[[str], FetchResult]


@dataclass(frozen=True)
class CrawlOptions:
    max_pages: int
    max_depth: int
    collect_emails: bool
    collect_socials: bool


def _header(result: FetchResult, name: str) -> str:
    for key, value in result.headers.items():
        if key.lower() == name:
            return value
    return ""


def _title_and_canonical(doc: Document, page_url: str) -> tuple[str | None, str | None]:
    title = next((i["text"] for i in doc.elements if i["tag"] == "title"), "")
    canonical = next(
        (
            i["attrs"].get("href")
            for i in doc.elements
            if i["tag"] == "link"
            and "canonical" in (i["attrs"].get("rel") or "").lower().split()
            and i["attrs"].get("href")
        ),
        "",
    )
    return (
        title[:_TEXT_LIMIT] or None,
        absolute(page_url, canonical) if canonical else None,
    )


class WebsiteCrawl:
    def __init__(
        self,
        *,
        buffer: RedisCrawlBuffer,
        fetch: FetchFn,
        website_id: str,
        website: str,
        options: CrawlOptions,
    ):
        self._buffer = buffer
        self._fetch = fetch
        self._website_id = website_id
        self._website = website
        self._options = options
        self._run_id: str | None = None
        self._origin = ""
        self._domain = ""
        self._home_url = ""
        self._home: FetchResult | None = None
        self._home_ms = 0
        self._home_state = "failed"
        self._home_error: str | None = None
        self._robots_text: str | None = None
        self._policy: RobotTxtPolicy | None = None
        self._sitemap_ok = False
        self._sitemap_count = 0
        self._known: set[str] = set()
        self._queue: deque[tuple[str, str, int]] = deque()
        self._counts = {"fetched": 0, "skipped": 0, "failed": 0}
        self._step_failed = False
        self._degraded = False
        self._truncated = False

    @property
    def run_id(self) -> str | None:
        return self._run_id

    def _active_run_id(self) -> str:
        if self._run_id is None:
            raise RuntimeError("the crawl run was not created")
        return self._run_id

    def run(self) -> str:
        """Crawl the website and return the final status; never raises."""
        try:
            self._origin = origin_of(self._website)
            self._domain = registrable_domain(hostname_of(self._origin))
            self._home_url = normalize(self._origin)
            self._run_id = self._buffer.create_run(
                website_id=self._website_id,
                requested_url=self._website,
                config=asdict(self._options),
            )
        except Exception as exc:
            logger.error(
                "crawl of %s could not start, no run was recorded: %s",
                self._website,
                describe(exc),
                exc_info=exc,
            )
            self._run_id = None
            return "failed"
        try:
            self._execute()
        except Exception as exc:  # noqa: BLE001 - run() never raises
            self._fail("run", exc)
        status = self._final_status()
        try:
            self._buffer.finish_run(
                self._run_id,
                status=status,
                progress={"pages": len(self._known), **self._counts},
                error_code=self._home_error if status == "failed" else None,
                error_message=None,
            )
        except Exception as exc:
            logger.error(
                "crawl of %s finished as %s but the run could not be closed: %s",
                self._website,
                status,
                describe(exc),
                exc_info=exc,
            )
        logger.info(
            "crawl of %s finished as %s: %s pages, %s fetched, %s skipped, %s failed",
            self._website,
            status,
            len(self._known),
            self._counts["fetched"],
            self._counts["skipped"],
            self._counts["failed"],
        )
        return status

    def _execute(self) -> None:
        self._step("robots", self._read_robots)
        self._step("fetch_home", self._fetch_home)
        if self._home_state != "ok":
            return
        self._step("sitemap", self._read_sitemaps)
        self._step("pages", self._crawl_pages)
        self._step("site", self._site_checks)

    def _step(self, name: str, action: Callable[[], None]) -> None:
        try:
            action()
        except Exception as exc:  # noqa: BLE001 - one failed step must not stop the others
            self._fail(name, exc)

    def _fail(self, step: str, exc: Exception, page_id: str | None = None) -> None:
        self._step_failed = True
        logger.error(
            "crawl of %s: step %s failed on page %s: %s",
            self._website,
            step,
            page_id or "-",
            describe(exc),
            exc_info=exc,
        )
        self._event(step, "error", f"{type(exc).__name__}: {exc}", page_id)

    def _event(
        self,
        step: str,
        level: str,
        message: str,
        page_id: str | None = None,
        data: dict | None = None,
    ) -> None:
        try:
            self._buffer.add_event(
                run_id=self._active_run_id(),
                step=step,
                level=level,
                message=message,
                page_id=page_id,
                data=data,
            )
        except Exception as exc:  # noqa: BLE001 - the event log must not stop the crawl
            logger.error(
                "crawl of %s: event %s/%s could not be recorded: %s",
                self._website,
                step,
                level,
                describe(exc),
            )

    def _final_status(self) -> str:
        if self._home_state == "failed":
            return "failed"
        if self._step_failed or self._degraded or self._home_state != "ok":
            return "partial"
        return "done"

    def _room(self) -> bool:
        return len(self._known) < self._options.max_pages

    def _seed(self, urls: list[str], depth: int, source: str) -> None:
        seeds = []
        for url in urls:
            try:
                normalized = normalize(url)
            except ValueError:
                continue
            if normalized in self._known:
                continue
            if not self._room():
                self._truncated = True
                continue
            self._known.add(normalized)
            seeds.append(PageSeed(url, normalized, depth, source))
        if not seeds:
            return
        ids = self._buffer.add_pages(self._active_run_id(), seeds)
        for seed in seeds:
            self._queue.append((seed.normalized_url, ids[seed.normalized_url], depth))

    def _skip_too_deep(self, url: str, depth: int) -> None:
        normalized = normalize(url)
        ids = self._buffer.add_pages(
            self._active_run_id(), [PageSeed(url, normalized, depth, "link")]
        )
        self._save(
            ids[normalized], PageResult(status="skipped", skip_reason="max_depth")
        )

    def _save(self, page_id: str, result: PageResult) -> None:
        self._buffer.save_page(page_id, result)
        self._counts[result.status] = self._counts.get(result.status, 0) + 1
        if result.status == "skipped" and result.skip_reason not in QUIET_SKIPS:
            self._degraded = True

    def _fetch_home(self) -> None:
        if self._policy is not None and not self._policy.is_allowed(self._home_url):
            self._home_state = "disallowed"
            self._seed([self._home_url], 0, "start")
            _, page_id, _ = self._queue.popleft()
            self._save(
                page_id, PageResult(status="skipped", skip_reason="robots_disallowed")
            )
            return
        self._seed([self._home_url], 0, "start")
        _, page_id, _ = self._queue[0]
        started = time.monotonic()
        try:
            result = self._fetch(self._home_url)
        except Exception as exc:  # noqa: BLE001 - a raising fetch is a failed homepage
            self._home_ms = int((time.monotonic() - started) * 1000)
            self._home_error = "fetch_exception"
            self._fail("fetch_home", exc, page_id)
            self._queue.clear()
            self._save(
                page_id,
                PageResult(
                    status="failed",
                    error_code="fetch_exception",
                    error_message=describe(exc)[:_TEXT_LIMIT],
                ),
            )
            return
        self._home_ms = int((time.monotonic() - started) * 1000)
        self._home = result
        if result.error == FetchError.REDIRECT_OUT_OF_SCOPE or not is_in_scope(
            result.final_url, self._domain
        ):
            self._home_state = "out_of_scope"
            self._queue.clear()
            self._event(
                "fetch_home",
                "warning",
                f"homepage redirects outside the site: {result.final_url}",
                data={"final_url": result.final_url},
            )
            self._save(
                page_id,
                PageResult(
                    status="skipped",
                    skip_reason="out_of_scope",
                    http_status=result.status or None,
                    final_url=result.final_url,
                ),
            )
        elif result.error is not None or not result.is_success:
            code = result.error.value if result.error else f"http_{result.status}"
            self._home_error = code
            self._queue.clear()
            self._event(
                "fetch_home",
                "error",
                f"homepage could not be fetched: {code}",
                data={"status": result.status, "detail": result.detail},
            )
            self._save(
                page_id,
                PageResult(
                    status="failed",
                    http_status=result.status or None,
                    final_url=result.final_url,
                    error_code=code,
                    error_message=result.detail[:_TEXT_LIMIT] or None,
                ),
            )
        else:
            self._home_state = "ok"

    def _read_robots(self) -> None:
        result = self._fetch(self._origin + "/robots.txt")
        if result.error is None and result.status == 200:
            self._robots_text = result.text
            self._policy = RobotTxtPolicy.from_text(self._robots_text)
        elif result.error is not None or result.status >= 500:
            self._event(
                "robots",
                "warning",
                "robots.txt could not be read; crawling without it",
                data={"status": result.status},
            )

    def _read_sitemaps(self) -> None:
        listed = self._policy.sitemaps if self._policy else []
        candidates = [url for url in listed if self._sitemap_wanted(url)]
        candidates = candidates[:MAX_SITEMAPS] or [self._origin + "/sitemap.xml"]
        found: dict[str, None] = {}
        nested: list[str] = []
        for url in candidates:
            urls, children = self._parse_sitemap_safely(url)
            found.update(dict.fromkeys(urls))
            nested.extend(children)
        for url in [u for u in nested if self._sitemap_wanted(u)][:MAX_SITEMAPS]:
            urls, _ = self._parse_sitemap_safely(url)
            found.update(dict.fromkeys(urls))
        self._sitemap_count = len(found)
        if self._options.max_depth >= 1:
            self._seed([u for u in found if is_in_scope(u, self._domain)], 1, "sitemap")

    def _sitemap_wanted(self, url: str) -> bool:
        return url.startswith(("http://", "https://")) and is_in_scope(
            url, self._domain
        )

    def _parse_sitemap_safely(self, url: str) -> tuple[list[str], list[str]]:
        try:
            return self._parse_sitemap(url)
        except Exception as exc:  # noqa: BLE001 - one failed sitemap must not drop the others
            self._fail("sitemap", exc)
            return [], []

    def _parse_sitemap(self, url: str) -> tuple[list[str], list[str]]:
        result = self._fetch(url)
        if result.error is not None or result.status != 200:
            return [], []
        try:
            urls, children = sitemap_urls(result.body)
        except ValueError as exc:
            self._event("sitemap", "warning", f"{url}: {exc}")
            return [], []
        self._sitemap_ok = True
        return urls, children

    def _crawl_pages(self) -> None:
        while self._queue:
            url, page_id, depth = self._queue.popleft()
            try:
                self._process_page(url, page_id, depth)
            except Exception as exc:  # noqa: BLE001 - one failed page must not stop the others
                self._fail("pages", exc, page_id)
                try:
                    self._save(
                        page_id,
                        PageResult(
                            status="failed",
                            error_code="crawler_error",
                            error_message=describe(exc)[:_TEXT_LIMIT],
                        ),
                    )
                except Exception as save_exc:  # noqa: BLE001 - keep crawling the other pages
                    logger.error(
                        "crawl of %s: page %s could not be marked failed: %s",
                        self._website,
                        page_id,
                        describe(save_exc),
                    )
        if self._truncated:
            self._degraded = True
            self._event(
                "pages",
                "warning",
                f"page limit of {self._options.max_pages} reached; some pages were not queued",
            )

    def _get(self, url: str) -> FetchResult | None:
        if url == self._home_url and self._home is not None:
            return self._home
        try:
            return self._fetch(url)
        except Exception as exc:
            logger.warning(
                "crawl of %s: fetching %s raised: %s",
                self._website,
                url,
                describe(exc),
                exc_info=exc,
            )
            self._event("pages", "warning", f"fetch raised {type(exc).__name__}: {exc}")
            return None

    def _process_page(self, url: str, page_id: str, depth: int) -> None:
        if self._policy is not None and not self._policy.is_allowed(url):
            self._save(
                page_id, PageResult(status="skipped", skip_reason="robots_disallowed")
            )
            return
        result = self._get(url)
        if result is None:
            self._save(
                page_id, PageResult(status="failed", error_code="fetch_exception")
            )
            return
        outcome = self._outcome(result)
        if outcome is not None:
            self._save(page_id, outcome)
            return
        base = result.final_url
        content_type = _header(result, "content-type")[:255] or None
        try:
            doc = parse_page(result.text, base)
            title, canonical = _title_and_canonical(doc, base)
        except Exception as exc:  # noqa: BLE001 - the page is still saved without a document
            self._fail("parse", exc, page_id)
            doc, title, canonical = None, None, None
        self._save(
            page_id,
            PageResult(
                status="fetched",
                http_status=result.status,
                content_type=content_type,
                content_length=len(result.body),
                final_url=base,
                title=title,
                canonical_url=canonical,
                content_hash=hashlib.sha256(result.body).hexdigest(),
            ),
        )
        if doc is None:
            return
        try:
            self._record_links(doc, base, page_id, depth)
        except Exception as exc:  # noqa: BLE001 - extractors still run without links
            self._fail("links", exc, page_id)
        self._run_extractors(doc, base, page_id)

    def _outcome(self, result: FetchResult) -> PageResult | None:
        """Return the stored result when the page is not usable, else ``None``."""
        shared = {
            "http_status": result.status or None,
            "final_url": result.final_url,
            "content_type": _header(result, "content-type")[:255] or None,
        }
        if result.error == FetchError.REDIRECT_OUT_OF_SCOPE or not is_in_scope(
            result.final_url, self._domain
        ):
            return PageResult(status="skipped", skip_reason="out_of_scope", **shared)
        if result.error == FetchError.UNSUPPORTED_CONTENT_TYPE:
            return PageResult(status="skipped", skip_reason="not_html", **shared)
        if result.error == FetchError.RESPONSE_TOO_LARGE:
            return PageResult(status="skipped", skip_reason="too_large", **shared)
        if result.error is not None or not result.is_success:
            code = result.error.value if result.error else f"http_{result.status}"
            return PageResult(
                status="failed",
                error_code=code,
                error_message=result.detail[:_TEXT_LIMIT] or None,
                **shared,
            )
        kind = _header(result, "content-type").split(";")[0].strip().lower()
        if kind and kind not in HTML_TYPES:
            return PageResult(status="skipped", skip_reason="not_html", **shared)
        if len(result.body) > MAX_BODY_BYTES:
            return PageResult(
                status="skipped",
                skip_reason="too_large",
                content_length=len(result.body),
                **shared,
            )
        return None

    def _record_links(self, doc: Document, base: str, page_id: str, depth: int) -> None:
        found: dict[str, tuple[str, str | None, str | None, bool]] = {}
        for item in doc.elements:
            href = item["attrs"].get("href")
            if item["tag"] != "a" or not href:
                continue
            try:
                target = absolute(base, href)
                if not target.startswith(("http://", "https://")):
                    continue
                normalized = normalize(target)
                internal = is_in_scope(target, self._domain)
            except ValueError as exc:
                logger.debug(
                    "crawl of %s: skipped malformed link %r on %s: %s",
                    self._website,
                    href,
                    base,
                    describe(exc),
                )
                continue
            if normalized not in found:
                found[normalized] = (
                    target,
                    item["text"][:_TEXT_LIMIT] or None,
                    item["attrs"].get("rel") or None,
                    internal,
                )
        ordered = sorted(found.items(), key=lambda entry: not entry[1][3])
        links = []
        for normalized, (target, anchor, rel, internal) in ordered:
            if normalized not in self._known:
                if internal and depth + 1 <= self._options.max_depth:
                    self._seed([target], depth + 1, "link")
                elif self._room():
                    if internal:
                        self._skip_too_deep(target, depth + 1)
                    self._known.add(normalized)
                else:
                    continue
                if normalized not in self._known:
                    continue
            links.append(LinkSeed(normalized, anchor, rel, internal))
        if links:
            self._buffer.add_links(page_id, links)

    def _run_extractors(self, doc: Document, page_url: str, page_id: str) -> None:
        extractors = [("contact", find_contact)]
        if self._options.collect_socials:
            extractors.append(("social", find_social))
        extractors += [
            ("tech", find_tech),
            ("business", find_business),
            ("seo", find_seo),
        ]
        findings: list[Finding] = []
        for name, extract in extractors:
            try:
                rows = extract(doc, page_url)
                if not self._options.collect_emails:
                    rows = [
                        r
                        for r in rows
                        if not (r.group == "contact" and r.key == "email")
                    ]
            except Exception as exc:  # noqa: BLE001 - one failed extractor leaves the others running
                self._fail(f"extract_{name}", exc, page_id)
                continue
            findings.extend(rows)
        if not findings:
            return
        try:
            self._buffer.add_findings(
                website_id=self._website_id,
                run_id=self._active_run_id(),
                page_id=page_id,
                findings=findings,
            )
        except Exception as exc:  # noqa: BLE001 - the crawl continues without these findings
            self._fail("save_findings", exc, page_id)

    def _site_checks(self) -> None:
        home = self._home
        if home is None:
            raise RuntimeError("the homepage was not fetched")
        findings = find_site_checks(
            home.status,
            home.final_url,
            self._home_ms,
            self._robots_text,
            self._sitemap_ok,
            self._sitemap_count,
        )
        self._buffer.add_findings(
            website_id=self._website_id,
            run_id=self._active_run_id(),
            page_id=None,
            findings=findings,
        )
