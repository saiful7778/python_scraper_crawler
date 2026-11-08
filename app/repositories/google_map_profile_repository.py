import json

from app.models import GoogleMapProfileModel, GoogleMapProfileToLeadModel
from app.services.google_map_scraper.job.job_types import LeadRow
from app.services.google_map_scraper.job.lead_rows import SOCIALS

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
    address = {
        "complete_address": row.get("complete_address", ""),
        "plus_code": row.get("plus_code", ""),
        "latitude": row.get("latitude"),
        "longitude": row.get("longitude"),
    }
    return {
        "emails": _split(row.get("emails", ""), ", "),
        "categories": _split(row.get("categories", ""), " | "),
        "socials": {k: row.get(k) for k in SOCIALS if row.get(k)},
        "address": {k: v for k, v in address.items() if v not in (None, "")},
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

    async def save_row(self, row: LeadRow) -> None:
        profile = await self._save_profile(row)
        lead = await self._save_lead(row)
        if lead is not None:
            await self._leads.add_children(lead, _children(row))
            await GoogleMapProfileToLeadModel.get_or_create(
                map_profile_id=profile.id, lead_id=lead.id
            )

    async def _find_profile(self, row: LeadRow):
        place_id = row.get("place_id")
        if not place_id:
            return None
        filters = {"place_id": place_id}
        if row.get("cid"):
            filters["cid"] = row["cid"]
        return await self._model.filter(**filters).first()

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

    async def _save_lead(self, row: LeadRow):
        domain = website_domain(row.get("website", ""))
        if domain is None:
            return None
        values = {name: row.get(name) or None for name in LEAD_FIELDS}
        lead = await self._leads.find_by_domain(domain)
        if lead is None:
            return await self._leads.create({**values, "website_domain": domain})
        if self.fill_empty(lead, values):
            await lead.save()
        return lead
