"""The crawler's website queue and crawl tables, stored in ``scrapes.db``.

:class:`CrawlStore` runs its calls on the database thread of the
:class:`ScrapeStore` it is given, so both share one event loop and one
Tortoise connection. The module-level coroutines are the crawl-table queries the
scrape store uses to clean up and read back a scrape's website data.
"""

from collections.abc import Coroutine, Sequence
from typing import TYPE_CHECKING, TypeVar

from tortoise import Tortoise

from app.services.google_map_scraper.scrape.scrape_models import (
    ScrapeLeadModel,
    ScrapeSettingsModel,
)
from app.services.google_map_scraper.scrape.scrape_status import CrawlStatus
from app.services.google_map_scraper.scrape.scrape_types import (
    WebsiteBatch,
    WebsiteTarget,
)

from .models import (
    WEBSITE_TECH_FIELDS,
    CrawlSocialModel,
    CrawlWebsiteModel,
)

if TYPE_CHECKING:
    from app.services.google_map_scraper.scrape.scrape_store import ScrapeStore

T = TypeVar("T")


def _website_values(website: CrawlWebsiteModel) -> dict[str, str]:
    """A lead's tech values and social links as one string per field; several links of a platform are joined."""
    values: dict[str, str] = {
        name: getattr(website, name)
        for name in WEBSITE_TECH_FIELDS
        if getattr(website, name) is not None
    }
    by_platform: dict[str, list[str]] = {}
    for social in website.socials.related_objects:
        by_platform.setdefault(social.platform, []).append(social.value)
    values.update(
        {platform: ", ".join(found) for platform, found in by_platform.items()}
    )
    return values


async def remove_scrape_websites(scrape_id: str) -> int:
    """Delete the crawl data of a scrape; return how many websites were deleted."""
    return await CrawlWebsiteModel.filter(scrape_id=scrape_id).delete()


async def remove_websites_from(scrape_id: str, count: int) -> None:
    """Delete the crawl data of every lead at position ``count`` or later."""
    await CrawlWebsiteModel.filter(
        scrape_id=scrape_id, lead_position__gte=count or 0
    ).delete()


async def select_website_values(scrape_id: str) -> dict[int, dict[str, str]]:
    """The tech values and social links the crawler found, keyed by lead position."""
    websites = await CrawlWebsiteModel.filter(scrape_id=scrape_id).prefetch_related(
        "socials"
    )
    return {website.lead_position: _website_values(website) for website in websites}


async def _queue_websites(scrape_id: str) -> int:
    leads = (
        await ScrapeLeadModel.filter(scrape_id=scrape_id)
        .exclude(website="")
        .values_list("id", "position")
    )
    queued = set(
        await CrawlWebsiteModel.filter(scrape_id=scrape_id).values_list(
            "lead_position", flat=True
        )
    )
    new = [
        CrawlWebsiteModel(
            scrape_id=scrape_id,
            lead_position=position,
            scrape_lead_id=lead_id,
            crawl_status=CrawlStatus.PENDING,
        )
        for lead_id, position in leads
        if position not in queued
    ]
    await CrawlWebsiteModel.bulk_create(new)
    return len(new)


async def _count_pending_websites(scrape_id: str) -> int:
    return await CrawlWebsiteModel.filter(
        scrape_id=scrape_id,
        crawl_status__in=[CrawlStatus.PENDING, CrawlStatus.RUNNING],
    ).count()


async def _skip_pending_websites(scrape_id: str) -> int:
    return await CrawlWebsiteModel.filter(
        scrape_id=scrape_id,
        crawl_status__in=[CrawlStatus.PENDING, CrawlStatus.RUNNING],
    ).update(crawl_status=CrawlStatus.SKIPPED)


_CLAIM_WEBSITES_SQL = """
UPDATE crawl_websites SET crawl_status = ?
WHERE scrape_id = ? AND crawl_status = ? AND id IN (
    SELECT id FROM crawl_websites
    WHERE scrape_id = ? AND crawl_status = ?
    ORDER BY created_at, lead_position
    LIMIT ?
)
RETURNING lead_position
"""


async def _next_website_batch(size: int) -> WebsiteBatch | None:
    first = (
        await CrawlWebsiteModel.filter(crawl_status=CrawlStatus.PENDING)
        .order_by("created_at", "lead_position")
        .first()
    )
    if first is None:
        return None
    scrape_id = first.scrape_id
    claimed = await Tortoise.get_connection("default").execute_query_dict(
        _CLAIM_WEBSITES_SQL,
        [
            CrawlStatus.RUNNING,
            scrape_id,
            CrawlStatus.PENDING,
            scrape_id,
            CrawlStatus.PENDING,
            size,
        ],
    )
    if not claimed:
        return await _next_website_batch(size)
    positions = [int(row["lead_position"]) for row in claimed]
    settings = await ScrapeSettingsModel.get(scrape_id=scrape_id)
    leads = {
        lead.position: lead
        for lead in await ScrapeLeadModel.filter(
            scrape_id=scrape_id, position__in=positions
        )
    }
    return WebsiteBatch(
        scrape_id=scrape_id,
        collect_emails=settings.collect_emails,
        collect_socials=settings.collect_socials,
        crawl_max_pages=settings.crawl_max_pages,
        crawl_max_depth=settings.crawl_max_depth,
        crawl_store_html=settings.crawl_store_html,
        websites=[
            WebsiteTarget(
                row_id=leads[position].id,
                position=position,
                website=leads[position].website,
            )
            for position in positions
            if position in leads
        ],
    )


async def _requeue_running_websites() -> int:
    return await CrawlWebsiteModel.filter(crawl_status=CrawlStatus.RUNNING).update(
        crawl_status=CrawlStatus.PENDING
    )


async def _save_website_result(
    scrape_id: str,
    position: int,
    fields: dict[str, str],
    socials: Sequence[tuple[str, str]],
) -> None:
    tech_values = {k: v for k, v in fields.items() if k in WEBSITE_TECH_FIELDS}
    updated = await CrawlWebsiteModel.filter(
        scrape_id=scrape_id,
        lead_position=position,
        crawl_status__in=[CrawlStatus.PENDING, CrawlStatus.RUNNING],
    ).update(crawl_status=CrawlStatus.DONE, **tech_values)
    if not updated:
        return
    website = await CrawlWebsiteModel.get(scrape_id=scrape_id, lead_position=position)
    if socials:
        await CrawlSocialModel.bulk_create(
            [
                CrawlSocialModel(website_id=website.id, platform=platform, value=value)
                for platform, value in socials
            ]
        )
    lead_values = {k: v for k, v in fields.items() if k not in WEBSITE_TECH_FIELDS}
    if lead_values:
        await ScrapeLeadModel.filter(scrape_id=scrape_id, position=position).update(
            **lead_values
        )


class CrawlStore:
    """Synchronous facade over the website queue and the crawl tables of a scrape store."""

    def __init__(self, store: "ScrapeStore"):
        """Use ``store``'s database thread for every call."""
        self._store = store

    def run_async(self, coroutine: Coroutine[object, object, T]) -> T:
        """Run a coroutine on the scrape store's database thread and wait for its result."""
        return self._store.run_async(coroutine)

    def delete_scrape(self, scrape_id: str) -> int:
        """Delete the crawl data of a scrape; return how many websites were deleted."""
        return self.run_async(remove_scrape_websites(scrape_id))

    def queue_websites(self, scrape_id: str) -> int:
        """Queue the leads of a scrape that have a website; returns how many were queued."""
        return self.run_async(_queue_websites(scrape_id))

    def count_pending_websites(self, scrape_id: str) -> int:
        """How many websites of a scrape still wait for the crawler."""
        return self.run_async(_count_pending_websites(scrape_id))

    def skip_pending_websites(self, scrape_id: str) -> int:
        """Give up on the websites of a scrape still waiting; returns how many."""
        return self.run_async(_skip_pending_websites(scrape_id))

    def next_website_batch(self, size: int) -> WebsiteBatch | None:
        """Claim the next leads to check, all from one scrape, or ``None`` when the queue is empty."""
        return self.run_async(_next_website_batch(size))

    def requeue_running_websites(self) -> int:
        """Put websites claimed by an earlier run back in the queue; returns how many."""
        return self.run_async(_requeue_running_websites())

    def save_website_result(
        self,
        scrape_id: str,
        position: int,
        fields: dict[str, str],
        socials: Sequence[tuple[str, str]] = (),
    ) -> None:
        """Store what the crawler found for a lead; ignored if the lead was skipped meanwhile."""
        self.run_async(_save_website_result(scrape_id, position, fields, socials))
