"""Stores a finished crawl in PostgreSQL and, when a lead matches, gives it the emails and socials found."""

from uuid import NAMESPACE_URL, UUID, uuid5

from tortoise.transactions import in_transaction

from app.models import (
    CrawlEventModel,
    CrawlFindingModel,
    CrawlLinkModel,
    CrawlPageModel,
    CrawlRunModel,
    CrawlWebsiteModel,
)
from app.repositories.lead_repository import LeadRepository
from app.services.buffer.crawl_types import WEBSITE_TECH_FIELDS, WebsiteSnapshot
from app.services.buffer.flusher import upsert
from app.services.crawler.findings import Finding
from app.services.crawler.lead_fields import lead_columns, social_links


def _event_id(run_id: object, event_number: object) -> UUID:
    return uuid5(NAMESPACE_URL, f"{run_id}:{event_number}")


def _findings_of(snapshot: WebsiteSnapshot) -> list[Finding]:
    return [
        Finding(
            group=row["group"], key=row["key"], value=row["value"], extra=row["extra"]
        )
        for row in snapshot.findings
    ]


def crawl_columns(snapshot: WebsiteSnapshot) -> dict[str, str]:
    """The lead columns of a crawl: emails and tech values.

    The crawler only records the emails and socials its scrape asked for, so
    nothing is filtered out here.
    """
    return lead_columns(_findings_of(snapshot), collect_emails=True)


def crawl_socials(snapshot: WebsiteSnapshot) -> list[tuple[str, str]]:
    """Every social profile a crawl found, as ``(platform, url)`` pairs."""
    return social_links(_findings_of(snapshot), collect_socials=True)


async def _stored_website_id(snapshot: WebsiteSnapshot) -> str:
    """The id of the origin's row in PostgreSQL, which may predate the origin-derived id."""
    existing = await CrawlWebsiteModel.get_or_none(origin=snapshot.website["origin"])
    return str(existing.id) if existing else snapshot.website["id"]


async def store_crawl(snapshot: WebsiteSnapshot) -> None:
    """Upsert a finished website and link it to its lead when one matches.

    A matching lead also gets the emails and socials the crawl found. Without
    a match the website is stored with no lead. Repeating it is harmless.
    """
    domain = snapshot.website["host"].removeprefix("www.")
    lead = await LeadRepository().find_by_host(domain)
    columns = crawl_columns(snapshot)
    first_per_platform: dict[str, str] = {}
    for platform, url in crawl_socials(snapshot):
        first_per_platform.setdefault(platform, url)
    events = [
        {**event, "id": _event_id(event["run_id"], event["id"])}
        for event in snapshot.events
    ]
    emails = [email for email in columns.get("emails", "").split(", ") if email]
    async with in_transaction():
        website_id = await _stored_website_id(snapshot)
        website = {
            **snapshot.website,
            "id": website_id,
            "lead_id": lead.id if lead is not None else None,
            **{name: columns[name] for name in WEBSITE_TECH_FIELDS if name in columns},
        }
        runs = [{**run, "website_id": website_id} for run in snapshot.runs]
        findings = [{**row, "website_id": website_id} for row in snapshot.findings]
        await upsert(CrawlWebsiteModel, [website])
        await upsert(CrawlRunModel, runs)
        await upsert(CrawlPageModel, snapshot.pages)
        await upsert(CrawlLinkModel, snapshot.links)
        await upsert(CrawlFindingModel, findings)
        await upsert(CrawlEventModel, events)
        if lead is not None:
            await LeadRepository().add_children(
                lead,
                {
                    "emails": emails,
                    "categories": [],
                    "socials": first_per_platform,
                    "address": {},
                },
            )
