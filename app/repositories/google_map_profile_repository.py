import json
from typing import cast

from app.constant import SOCIALS
from app.models import GoogleMapProfileModel, GoogleMapProfileToLeadModel
from app.services.google_map_scraper.scrape.lead_rows import address_fields
from app.services.google_map_scraper.scrape.scrape_types import LeadRow

from .base import BaseRepository
from .lead_repository import LeadRepository, website_domain

PROFILE_TEXT_FIELDS = (
    "input_id",
    "data_id",
    "place_id",
    "cid",
    "link",
    "price_range",
    "credit_cards_accepted",
    "reviews_link",
    "street_view_url",
    "reviews_per_rating",
    "open_hours",
    "popular_times",
    "thumbnail",
    "reservations",
    "order_online",
    "menu",
    "images",
    "user_reviews",
    "user_reviews_extended",
)

LEAD_FIELDS = (
    "title",
    "category",
    "address",
    "phone",
    "website",
    "status",
    "descriptions",
    "owner",
    "timezone",
)

KEY_FIELDS = ("data_id", "place_id", "title")
PROFILE_KEY_FIELDS = tuple(f for f in KEY_FIELDS if f in PROFILE_TEXT_FIELDS)
LEAD_KEY_FIELDS = tuple(f for f in KEY_FIELDS if f in LEAD_FIELDS)


def _number(text, kind):
    if not text:
        return None
    try:
        return kind(str(text).replace(",", ""))
    except ValueError:
        return None


def _about(text):
    if not text:
        return None
    try:
        return json.loads(text)
    except ValueError:
        return text


def _split(text: str, separator: str) -> list[str]:
    return [part.strip() for part in text.split(separator) if part.strip()]


def _profile_values(row: LeadRow) -> dict:
    values = {name: row.get(name, "") for name in PROFILE_TEXT_FIELDS}
    values["about"] = _about(row.get("about", ""))
    values["review_count"] = _number(row.get("review_count", ""), int)
    values["review_rating"] = _number(row.get("review_rating", ""), float)
    return values


def _children(row: LeadRow) -> dict:
    return {
        "categories": _split(row.get("categories", ""), " | "),
        "socials": {k: row.get(k) for k in SOCIALS if row.get(k)},
        "address": address_fields(row),
    }


class GoogleMapProfileRepository(BaseRepository):
    def __init__(self, leads: LeadRepository):
        self._leads = leads
        self._model = GoogleMapProfileModel
        self._counters = {}

    async def _next_global_counter(self):
        value = self._counters.get(self._model)
        if value is None:
            value = (await self.last_counter(self._model)) or 0
        value += 1
        self._counters[self._model] = value
        return value

    async def key_rows(self) -> list[LeadRow]:
        """The duplicate-check key fields of every stored profile and linked lead, and nothing else."""
        profiles = await self._model.all().values(*PROFILE_KEY_FIELDS)
        leads = await GoogleMapProfileToLeadModel.all().values(
            *(f"lead__{field}" for field in LEAD_KEY_FIELDS)
        )
        return [
            *(cast(LeadRow, row) for row in profiles),
            *(
                cast(
                    LeadRow, {field: row[f"lead__{field}"] for field in LEAD_KEY_FIELDS}
                )
                for row in leads
            ),
        ]

    async def save_row(self, row: LeadRow, scrape_id: str) -> None:
        profile = await self._save_profile(row)
        lead = await self._save_lead(row, profile)
        await self._leads.add_children(lead, _children(row))
        await GoogleMapProfileToLeadModel.get_or_create(
            map_profile_id=profile.id, lead_id=lead.id
        )

    async def _find_profile(self, row: LeadRow):
        place_id = row.get("place_id")
        if not place_id:
            return await self._find_profile_by_lead(row)
        filters = {"place_id": place_id}
        if row.get("cid"):
            filters["cid"] = row["cid"]
        return await self._model.filter(**filters).first()

    async def _find_profile_by_lead(self, row: LeadRow):
        """The profile of a lead saved earlier with the same title and website."""
        title = row.get("title")
        if not title:
            return None
        domain = website_domain(row.get("website", ""))
        link = (
            await GoogleMapProfileToLeadModel.filter(
                lead__title=title,
                **(
                    {"lead__website_domain": domain}
                    if domain
                    else {"lead__website_domain__isnull": True}
                ),
            )
            .first()
            .prefetch_related("map_profile")
        )
        return link.map_profile if link else None

    async def _save_profile(self, row: LeadRow):
        values = _profile_values(row)
        profile = await self._find_profile(row)
        if profile is None:
            return await self._model.create(
                counter=await self._next_global_counter(), **values
            )
        if self.fill_empty(profile, values):
            await profile.save()
        return profile

    async def _linked_lead(self, profile):
        link = (
            await GoogleMapProfileToLeadModel.filter(map_profile_id=profile.id)
            .first()
            .prefetch_related("lead")
        )
        return link.lead if link else None

    async def _save_lead(self, row: LeadRow, profile):
        domain = website_domain(row.get("website", ""))
        values = {name: row.get(name) or None for name in LEAD_FIELDS}
        if domain is None:
            lead = await self._linked_lead(profile)
            if lead is None:
                return await self._leads.create({**values, "website_domain": None})
        else:
            lead = await self._leads.find_by_domain(domain)
            if lead is None:
                return await self._leads.create({**values, "website_domain": domain})
        if self.fill_empty(lead, values):
            await lead.save()
        return lead
