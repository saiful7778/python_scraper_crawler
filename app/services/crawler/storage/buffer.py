"""Synchronous write and read API over the SQLite crawl buffer tables.

The crawler thread records everything it finds through :class:`CrawlBuffer`;
the Postgres sync reads one finished website at a time as a
:class:`WebsiteSnapshot` and then deletes it from the buffer.
"""

from dataclasses import dataclass, field
from typing import Any, TypeVar

from tortoise.timezone import now
from tortoise.transactions import in_transaction

from app.utils.logger import get_logger

from ..findings import Finding
from .crawl_store import CrawlStore
from .models import (
    CrawlEventModel,
    CrawlFindingModel,
    CrawlLinkModel,
    CrawlPageModel,
    CrawlRawHTMLModel,
    CrawlRunModel,
    CrawlWebsiteModel,
)

logger = get_logger(__name__)

T = TypeVar("T")

FINISHED_STATUSES = ("done", "partial", "failed")
_RUNNING = "running"
_QUEUED = "queued"
_SKIPPED = "skipped"
_UNCRAWLED_LINK_REASON = "out_of_scope"
_INTERRUPTED = "interrupted"
#: Website columns the Postgres copy leaves out: they are scrape-side state only.
_LOCAL_WEBSITE_COLUMNS = ("scrape_lead_id", "crawl_status", "copied")


@dataclass(frozen=True)
class PageSeed:
    url: str
    normalized_url: str
    depth: int
    discovery_source: str


@dataclass(frozen=True)
class PageResult:
    status: str
    skip_reason: str | None = None
    http_status: int | None = None
    content_type: str | None = None
    content_length: int | None = None
    final_url: str | None = None
    title: str | None = None
    canonical_url: str | None = None
    content_hash: str | None = None
    error_code: str | None = None
    error_message: str | None = None
    html: str | None = None


@dataclass(frozen=True)
class LinkSeed:
    target_normalized_url: str
    anchor_text: str | None
    rel: str | None
    is_internal: bool


@dataclass
class WebsiteSnapshot:
    """Every buffered row of one website, as dicts keyed by the Postgres field names."""

    website: dict[str, Any]
    runs: list[dict[str, Any]] = field(default_factory=list)
    pages: list[dict[str, Any]] = field(default_factory=list)
    links: list[dict[str, Any]] = field(default_factory=list)
    raw_html: list[dict[str, Any]] = field(default_factory=list)
    findings: list[dict[str, Any]] = field(default_factory=list)
    events: list[dict[str, Any]] = field(default_factory=list)


async def _create_website(
    scrape_id: str,
    lead_position: int,
    scrape_lead_id: int,
    host: str,
    registrable_domain: str,
    origin: str,
) -> str:
    website, _ = await CrawlWebsiteModel.update_or_create(
        scrape_id=scrape_id,
        lead_position=lead_position,
        defaults={
            "scrape_lead_id": scrape_lead_id,
            "host": host,
            "registrable_domain": registrable_domain,
            "origin": origin,
            "status": _RUNNING,
            "copied": False,
            "error_code": None,
            "error_message": None,
            "finished_at": None,
        },
    )
    return str(website.id)


async def _create_run(website_id: str, requested_url: str, config: dict) -> str:
    run = await CrawlRunModel.create(
        website_id=website_id,
        requested_url=requested_url,
        status=_RUNNING,
        config=config,
        started_at=now(),
    )
    return str(run.id)


async def _insert_pages(run_id: str, pages: list[PageSeed]) -> dict[str, str]:
    wanted = {page.normalized_url for page in pages}
    existing = {
        page.normalized_url: page
        for page in await CrawlPageModel.filter(
            run_id=run_id, normalized_url__in=list(wanted)
        )
    }
    for seed in pages:
        if seed.normalized_url not in existing:
            existing[seed.normalized_url] = await CrawlPageModel.create(
                run_id=run_id,
                url=seed.url,
                normalized_url=seed.normalized_url,
                depth=seed.depth,
                status=_QUEUED,
                discovery_source=seed.discovery_source,
            )
    return {url: str(page.id) for url, page in existing.items() if url in wanted}


async def _write_page(page_id: str, result: PageResult) -> None:
    async with in_transaction():
        await CrawlPageModel.filter(id=page_id).update(
            status=result.status,
            skip_reason=result.skip_reason,
            http_status=result.http_status,
            content_type=result.content_type,
            content_length=result.content_length,
            final_url=result.final_url,
            title=result.title,
            canonical_url=result.canonical_url,
            content_hash=result.content_hash,
            error_code=result.error_code,
            error_message=result.error_message,
            fetched_at=now() if result.status == "fetched" else None,
        )
        if result.html is not None:
            await CrawlRawHTMLModel.update_or_create(
                page_id=page_id, defaults={"html": result.html}
            )


async def _insert_links(source_page_id: str, links: list[LinkSeed]) -> None:
    source = await CrawlPageModel.get(id=source_page_id)
    async with in_transaction():
        targets = {
            page.normalized_url: page
            for page in await CrawlPageModel.filter(
                run_id=source.run_id,
                normalized_url__in=[link.target_normalized_url for link in links],
            )
        }
        linked = {
            str(target_id)
            for target_id in await CrawlLinkModel.filter(
                source_id=source.id
            ).values_list("target_id", flat=True)
        }
        for link in links:
            target = targets.get(link.target_normalized_url)
            if target is None:
                target = targets[
                    link.target_normalized_url
                ] = await CrawlPageModel.create(
                    run_id=source.run_id,
                    url=link.target_normalized_url,
                    normalized_url=link.target_normalized_url,
                    depth=source.depth,
                    status=_SKIPPED,
                    skip_reason=_UNCRAWLED_LINK_REASON,
                    discovery_source="link",
                )
            if str(target.id) in linked:
                continue
            linked.add(str(target.id))
            await CrawlLinkModel.create(
                source_id=source.id,
                target_id=target.id,
                anchor_text=link.anchor_text,
                rel=link.rel,
                is_internal=link.is_internal,
            )


async def _insert_findings(
    website_id: str, run_id: str, page_id: str | None, findings: list[Finding]
) -> None:
    async with in_transaction():
        await CrawlFindingModel.bulk_create(
            CrawlFindingModel(
                website_id=website_id,
                run_id=run_id,
                page_id=page_id,
                group=finding.group,
                key=finding.key,
                value=finding.value,
                extra=finding.extra,
            )
            for finding in findings
        )


async def _insert_event(
    run_id: str,
    step: str,
    level: str,
    message: str,
    page_id: str | None,
    data: dict | None,
) -> None:
    await CrawlEventModel.create(
        run_id=run_id,
        page_id=page_id,
        step=step,
        level=level,
        message=message,
        data=data,
    )


async def _close_run(
    run_id: str,
    status: str,
    progress: dict,
    error_code: str | None,
    error_message: str | None,
) -> None:
    run = await CrawlRunModel.get(id=run_id)
    finished = now()
    async with in_transaction():
        await CrawlRunModel.filter(id=run_id).update(
            status=status,
            progress=progress,
            error_code=error_code,
            error_message=error_message,
            finished_at=finished,
        )
        await CrawlWebsiteModel.filter(id=run.website_id).update(
            status=status,
            error_code=error_code,
            error_message=error_message,
            finished_at=finished,
        )


async def _fail_website(website_id: str, error_code: str, error_message: str) -> None:
    await CrawlWebsiteModel.filter(id=website_id).update(
        status="failed",
        error_code=error_code,
        error_message=error_message,
        finished_at=now(),
    )


async def _mark_interrupted() -> tuple[int, int]:
    finished = now()
    websites = await CrawlWebsiteModel.filter(status=_RUNNING).update(
        status="failed",
        error_code=_INTERRUPTED,
        finished_at=finished,
    )
    runs = await CrawlRunModel.filter(status__in=[_RUNNING, _QUEUED]).update(
        status="failed",
        error_code=_INTERRUPTED,
        finished_at=finished,
    )
    return websites, runs


async def _select_finished_website_ids() -> list[str]:
    ids = (
        await CrawlWebsiteModel.filter(status__in=FINISHED_STATUSES, copied=False)
        .order_by("created_at")
        .values_list("id", flat=True)
    )
    return [str(website_id) for website_id in ids]


async def _select_website(website_id: str) -> WebsiteSnapshot:
    website = await CrawlWebsiteModel.filter(id=website_id).values()
    if not website:
        raise LookupError(f"crawl website {website_id} is not in the buffer")
    website = {k: v for k, v in website[0].items() if k not in _LOCAL_WEBSITE_COLUMNS}
    runs = (
        await CrawlRunModel.filter(website_id=website_id)
        .order_by("created_at")
        .values()
    )
    run_ids = [run["id"] for run in runs]
    pages = (
        await CrawlPageModel.filter(run_id__in=run_ids)
        .order_by("created_at", "normalized_url")
        .values()
    )
    page_ids = [page["id"] for page in pages]
    return WebsiteSnapshot(
        website=website,
        runs=runs,
        pages=pages,
        links=await CrawlLinkModel.filter(source_id__in=page_ids)
        .order_by("created_at")
        .values(),
        raw_html=await CrawlRawHTMLModel.filter(page_id__in=page_ids).values(),
        findings=await CrawlFindingModel.filter(website_id=website_id)
        .order_by("created_at")
        .values(),
        events=await CrawlEventModel.filter(run_id__in=run_ids).order_by("id").values(),
    )


async def _release_website(website_id: str) -> None:
    async with in_transaction():
        await CrawlRunModel.filter(website_id=website_id).delete()
        await CrawlWebsiteModel.filter(id=website_id).update(copied=True)


class CrawlBuffer:
    """Records one crawl in SQLite until it is copied to Postgres."""

    def __init__(self, store: CrawlStore):
        """Use ``store``'s database thread for every call."""
        self._store = store

    def create_website(
        self,
        *,
        scrape_id: str,
        lead_position: int,
        scrape_lead_id: int,
        host: str,
        registrable_domain: str,
        origin: str,
    ) -> str:
        """Add a running website for a lead and return its id."""
        return self._store.run_async(
            _create_website(
                scrape_id,
                lead_position,
                scrape_lead_id,
                host,
                registrable_domain,
                origin,
            )
        )

    def create_run(self, *, website_id: str, requested_url: str, config: dict) -> str:
        """Add a running crawl of a website and return its id."""
        return self._store.run_async(_create_run(website_id, requested_url, config))

    def add_pages(self, run_id: str, pages: list[PageSeed]) -> dict[str, str]:
        """Queue pages; return normalized URL to page id, reusing existing pages."""
        return self._store.run_async(_insert_pages(run_id, pages))

    def save_page(self, page_id: str, result: PageResult) -> None:
        """Store the outcome of fetching a page, and its HTML when given."""
        self._store.run_async(_write_page(page_id, result))

    def add_links(self, source_page_id: str, links: list[LinkSeed]) -> None:
        """Record links found on a page; unknown targets become skipped pages."""
        self._store.run_async(_insert_links(source_page_id, links))

    def add_findings(
        self,
        *,
        website_id: str,
        run_id: str,
        page_id: str | None,
        findings: list[Finding],
    ) -> None:
        """Store extracted values for a page, or for the whole site when ``page_id`` is None."""
        self._store.run_async(_insert_findings(website_id, run_id, page_id, findings))

    def add_event(
        self,
        *,
        run_id: str,
        step: str,
        level: str,
        message: str,
        page_id: str | None = None,
        data: dict | None = None,
    ) -> None:
        """Log a step of the crawl."""
        self._store.run_async(
            _insert_event(run_id, step, level, message, page_id, data)
        )

    def finish_run(
        self,
        run_id: str,
        *,
        status: str,
        progress: dict,
        error_code: str | None,
        error_message: str | None,
    ) -> None:
        """End a run and give its website the same status."""
        self._store.run_async(
            _close_run(run_id, status, progress, error_code, error_message)
        )

    def fail_website(
        self, website_id: str, error_code: str, error_message: str
    ) -> None:
        """Mark a website failed when no run of it could record the outcome."""
        self._store.run_async(_fail_website(website_id, error_code, error_message))

    def mark_interrupted(self) -> None:
        """Fail every website and run left running by an earlier process."""
        websites, runs = self._store.run_async(_mark_interrupted())
        if websites or runs:
            logger.warning(
                "marked %s website(s) and %s run(s) left unfinished by an earlier process as interrupted",
                websites,
                runs,
            )

    def finished_website_ids(self) -> list[str]:
        """Websites whose crawl has ended and that wait for the Postgres copy."""
        return self._store.run_async(_select_finished_website_ids())

    def read_website(self, website_id: str) -> WebsiteSnapshot:
        """Read every buffered row of a website.

        Raises:
            LookupError: the website is not in the buffer.
        """
        return self._store.run_async(_select_website(website_id))

    def release_website(self, website_id: str) -> None:
        """Drop the crawl data of a copied website; its row stays for the scrape's queue and exports."""
        self._store.run_async(_release_website(website_id))
