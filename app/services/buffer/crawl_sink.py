"""Stores a finished crawl in PostgreSQL and gives its lead the emails and socials found."""

from uuid import NAMESPACE_URL, UUID, uuid5

from tortoise.transactions import in_transaction

from app.models import (
    CrawlEventModel,
    CrawlFindingModel,
    CrawlLinkModel,
    CrawlPageModel,
    CrawlRunModel,
    CrawlWebsiteModel,
    LeadModel,
)
from app.repositories.lead_repository import LeadRepository
from app.services.buffer.crawl_types import WEBSITE_TECH_FIELDS, WebsiteSnapshot
from app.services.buffer.flusher import upsert
from app.services.crawler.findings import Finding
from app.services.crawler.lead_fields import lead_columns, social_links
from app.utils.errors import ScraperError


class LeadNotSavedError(ScraperError):
    """The lead of a crawled website is not in PostgreSQL yet; the flusher retries."""


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


async def _lead_of(snapshot: WebsiteSnapshot) -> LeadModel:
    domain = snapshot.website["host"].removeprefix("www.")
    lead = await LeadModel.get_or_none(website_domain=domain)
    if lead is None:
        raise LeadNotSavedError(f"the lead of {domain} is not in PostgreSQL yet")
    return lead


async def store_crawl(snapshot: WebsiteSnapshot) -> None:
    """Upsert a finished website, link it to its lead and add the lead's emails and socials.

    Repeating it is harmless.

    :raises LeadNotSavedError: The lead is not saved yet; nothing is written.
    """
    lead = await _lead_of(snapshot)
    columns = crawl_columns(snapshot)
    first_per_platform: dict[str, str] = {}
    for platform, url in crawl_socials(snapshot):
        first_per_platform.setdefault(platform, url)
    website = {
        **snapshot.website,
        "lead_id": lead.id,
        **{name: columns[name] for name in WEBSITE_TECH_FIELDS if name in columns},
    }
    events = [
        {**event, "id": _event_id(event["run_id"], event["id"])}
        for event in snapshot.events
    ]
    emails = [email for email in columns.get("emails", "").split(", ") if email]
    async with in_transaction():
        await upsert(CrawlWebsiteModel, [website])
        await upsert(CrawlRunModel, snapshot.runs)
        await upsert(CrawlPageModel, snapshot.pages)
        await upsert(CrawlLinkModel, snapshot.links)
        await upsert(CrawlFindingModel, snapshot.findings)
        await upsert(CrawlEventModel, events)
        await LeadRepository().add_children(
            lead,
            {
                "emails": emails,
                "categories": [],
                "socials": first_per_platform,
                "address": {},
            },
        )
