from urllib.parse import urlsplit
from uuid import UUID

from app.models import (
    LeadAddressModel,
    LeadCategoryModel,
    LeadEmailModel,
    LeadModel,
    LeadSocialModel,
)
from app.utils.errors import ScraperError, describe

from .base import BaseRepository


class LeadPersistenceError(ScraperError):
    """The child rows of a lead could not be saved. The message names the lead."""


def website_domain(website: str) -> str | None:
    """The host of a website, lowercased and without ``www.``; ``None`` when there is none."""
    text = website.strip()
    if not text:
        return None
    if "://" not in text:
        text = "http://" + text
    host = urlsplit(text).hostname
    return host.removeprefix("www.") if host else None


class LeadRepository(BaseRepository):
    def __init__(self):
        self._model = LeadModel
        self._counters = {}

    async def _next_global_counter(self):
        value = self._counters.get(self._model)
        if value is None:
            value = (await self.last_counter(self._model)) or 0
        value += 1
        self._counters[self._model] = value
        return value

    async def find_by_domain(self, domain: str):
        return await self._model.get_or_none(website_domain=domain)

    async def find_by_host(self, host: str) -> LeadModel | None:
        lead = await self._model.get_or_none(website_domain=host)
        if lead is not None:
            return lead
        for candidate in await self._model.filter(website__icontains=host):
            if website_domain(candidate.website) == host:
                return candidate
        return None

    async def all(self, descending: bool = False) -> list[LeadModel]:
        order = "-counter" if descending else "counter"
        return (
            await self._model.all()
            .order_by(order)
            .prefetch_related("emails", "categories")
        )

    async def find_details(self, lead_id: UUID) -> LeadModel | None:
        return await self._model.get_or_none(id=lead_id).prefetch_related(
            "emails",
            "categories",
            "socials",
            "crawl_websites__runs__pages",
            "crawl_websites__findings",
        )

    async def find_address(self, lead_id: UUID) -> LeadAddressModel | None:
        return await LeadAddressModel.get_or_none(lead_id=lead_id)

    async def delete(self, lead_id: UUID) -> bool:
        return await self._model.filter(id=lead_id).delete() > 0

    async def create(self, values: dict):
        return await self._model.create(
            counter=await self._next_global_counter(), **values
        )

    async def add_children(self, lead, parts):
        try:
            await self._add_children(lead, parts)
        except Exception as error:
            raise LeadPersistenceError(
                f"could not save the child rows of lead {lead.id}: {describe(error)}"
            ) from error

    async def _add_children(self, lead, parts):
        pid = lead.id
        await self.insert_ignoring_conflicts(
            LeadEmailModel,
            [LeadEmailModel(lead_id=pid, email=e) for e in parts.get("emails", [])],
        )
        await self.insert_ignoring_conflicts(
            LeadCategoryModel,
            [LeadCategoryModel(lead_id=pid, name=c) for c in parts["categories"]],
        )
        await self.insert_ignoring_conflicts(
            LeadSocialModel,
            [
                LeadSocialModel(lead_id=pid, platform=k, url=v)
                for k, v in parts["socials"].items()
            ],
        )
        if parts["address"]:
            scan = await LeadAddressModel.get_or_none(lead_id=pid)
            if scan is None:
                await LeadAddressModel.create(lead_id=pid, **parts["address"])
            elif self.fill_empty(scan, parts["address"]):
                await scan.save()
