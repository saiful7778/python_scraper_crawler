from urllib.parse import urlsplit

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
        base_email = await self.next_counter(LeadEmailModel, lead_id=pid)
        await self.insert_ignoring_conflicts(
            LeadEmailModel,
            [
                LeadEmailModel(lead_id=pid, email=e, counter=base_email + i)
                for i, e in enumerate(parts["emails"])
            ],
        )
        base_category = await self.next_counter(LeadCategoryModel, lead_id=pid)
        await self.insert_ignoring_conflicts(
            LeadCategoryModel,
            [
                LeadCategoryModel(lead_id=pid, name=c, counter=base_category + i)
                for i, c in enumerate(parts["categories"])
            ],
        )
        base_social = await self.next_counter(LeadSocialModel, lead_id=pid)
        await self.insert_ignoring_conflicts(
            LeadSocialModel,
            [
                LeadSocialModel(lead_id=pid, platform=k, url=v, counter=base_social + i)
                for i, (k, v) in enumerate(parts["socials"].items())
            ],
        )
        if parts["address"]:
            scan = await LeadAddressModel.get_or_none(lead_id=pid)
            if scan is None:
                await LeadAddressModel.create(lead_id=pid, **parts["address"])
            elif self.fill_empty(scan, parts["address"]):
                await scan.save()
