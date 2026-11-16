import hashlib
import json
from typing import cast
from uuid import UUID

from app.constant import SOCIALS
from app.models import (
    GoogleMapKeyword,
    GoogleMapProfileHourModel,
    GoogleMapProfileImageModel,
    GoogleMapProfileModel,
    GoogleMapProfileReviewModel,
    KeywordModel,
)
from app.services.google_map_scraper.scrape.lead_rows import address_fields
from app.services.google_map_scraper.scrape.scrape_types import LeadRow
from app.services.postgres import CONNECTION_LOST
from app.utils.errors import describe
from app.utils.logger import get_logger

from .base import BaseRepository
from .lead_repository import LeadRepository, website_domain

logger = get_logger(__name__)

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
    "popular_times",
    "thumbnail",
    "menu",
)
PROFILE_JSON_FIELDS = ("open_hours", "reservations", "order_online")

LEAD_FIELDS = (
    "title",
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


def _json(text):
    if not text:
        return None
    try:
        return json.loads(text)
    except ValueError:
        return None


def _profile_values(row: LeadRow) -> dict:
    values = {name: row.get(name, "") for name in PROFILE_TEXT_FIELDS}
    values.update({name: _json(row.get(name, "")) for name in PROFILE_JSON_FIELDS})
    values["about"] = _about(row.get("about", ""))
    values["review_count"] = _number(row.get("review_count", ""), int)
    values["review_rating"] = _number(row.get("review_rating", ""), float)
    for star in range(1, 6):
        key = f"review_star_{star}_count"
        values[key] = _number(row.get(key, ""), int)
    return values


def _list(text):
    items = _json(text)
    return items if isinstance(items, list) else []


def _hour_spans(open_hours) -> list[tuple[str, int, str]]:
    """The (day, position, span) triples of an open-hours record, one per time range."""
    if not isinstance(open_hours, dict):
        return []
    return [
        (day, position, span)
        for day, spans in open_hours.items()
        if isinstance(spans, list)
        for position, span in enumerate(spans)
        if isinstance(span, str) and span
    ]


def review_hash(review: dict) -> str:
    return hashlib.sha256(
        json.dumps(review, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()


def _children(row: LeadRow) -> dict:
    return {
        "categories": _split(row.get("categories", ""), " | "),
        "socials": {k: row.get(k) for k in SOCIALS if row.get(k)},
        "address": address_fields(row),
    }


class GoogleMapProfileRepository(BaseRepository):
    def __init__(self, leads: LeadRepository):
        super().__init__()
        self._leads = leads
        self._model = GoogleMapProfileModel
        self._keyword_ids: dict[str, UUID] = {}

    async def key_rows(self) -> list[LeadRow]:
        """The duplicate-check key fields of every stored profile and linked lead, and nothing else."""
        profiles = await self._model.all().values(*PROFILE_KEY_FIELDS)
        leads = await self._model.all().values(
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
        profile = await self._save_parent(row)
        await self._save_children([(row, profile)], scrape_id)

    async def save_rows(self, rows: list[LeadRow], scrape_id: str) -> int:
        """Save rows in order and return how many were saved from the start.

        Each lead and profile is saved one by one, because a row can match one
        stored earlier in the same batch. The keywords, hours, images and reviews
        of all saved rows are then written in one bulk insert per table. A row
        that fails stops the batch; the rows before it are kept.
        """
        saved: list[tuple[LeadRow, object]] = []
        for row in rows:
            try:
                saved.append((row, await self._save_parent(row)))
            except CONNECTION_LOST:
                raise
            except Exception as error:
                logger.error(
                    "saving a lead of scrape %s failed; it and the rows after it stay in Redis: %s",
                    scrape_id,
                    describe(error),
                    exc_info=error,
                )
                break
        await self._save_children(saved, scrape_id)
        return len(saved)

    async def _save_parent(self, row: LeadRow):
        existing = await self._find_profile(row)
        lead = await self._save_lead(row, existing)
        await self._leads.add_children(lead, _children(row))
        return await self._save_profile(row, lead, existing)

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
        return await self._model.filter(
            lead__title=title,
            **(
                {"lead__website_domain": domain}
                if domain
                else {"lead__website_domain__isnull": True}
            ),
        ).first()

    async def _save_profile(self, row: LeadRow, lead, profile):
        values = _profile_values(row)
        if profile is None:
            return await self._model.create(
                counter=await self._next_global_counter(), lead=lead, **values
            )
        self.fill_empty(profile, values)
        profile.lead = lead
        await profile.save()
        return profile

    async def _keyword_id(self, name: str) -> UUID:
        key = name.lower()
        keyword_id = self._keyword_ids.get(key)
        if keyword_id is None:
            keyword, _ = await KeywordModel.get_or_create(
                name_key=key,
                defaults={"name": name},
            )
            keyword_id = self._keyword_ids[key] = keyword.id
        return keyword_id

    async def _save_children(
        self, saved: list[tuple[LeadRow, object]], scrape_id: str
    ) -> None:
        keywords: list[GoogleMapKeyword] = []
        hours: list[GoogleMapProfileHourModel] = []
        images: list[GoogleMapProfileImageModel] = []
        reviews: list[GoogleMapProfileReviewModel] = []
        for row, profile in saved:
            name = row.get("input_id", "").strip()
            if name:
                keywords.append(
                    GoogleMapKeyword(
                        google_map_id=profile.id,
                        keyword_id=await self._keyword_id(name),
                        scrape_id=scrape_id,
                    )
                )
            hours += [
                GoogleMapProfileHourModel(
                    google_map_id=profile.id, day=day, position=position, span=span
                )
                for day, position, span in _hour_spans(_json(row.get("open_hours", "")))
            ]
            images += [
                GoogleMapProfileImageModel(
                    google_map_id=profile.id,
                    url=image["image"],
                    title=image.get("title") or None,
                )
                for image in _list(row.get("images", ""))
                if isinstance(image, dict) and image.get("image")
            ]
            reviews += [
                GoogleMapProfileReviewModel(
                    google_map_id=profile.id,
                    rating=review.get("Rating"),
                    when_text=review.get("When") or None,
                    description=review.get("Description") or None,
                    content_hash=review_hash(review),
                )
                for review in _list(row.get("user_reviews", ""))
                if isinstance(review, dict)
            ]
        await self.insert_ignoring_conflicts(GoogleMapKeyword, keywords)
        await self.insert_ignoring_conflicts(GoogleMapProfileHourModel, hours)
        await self.insert_ignoring_conflicts(GoogleMapProfileImageModel, images)
        await self.insert_ignoring_conflicts(GoogleMapProfileReviewModel, reviews)

    async def _linked_lead(self, profile):
        if profile is None:
            return None
        await profile.fetch_related("lead")
        return profile.lead

    async def _save_lead(self, row: LeadRow, profile):
        values = {name: row.get(name) or None for name in LEAD_FIELDS}
        lead = await self._linked_lead(profile)
        if lead is None:
            domain = await self._leads.find_by_domain(
                website_domain(row.get("website", ""))
            )
            return await self._leads.create({**values, "website_domain": domain})
        if self.fill_empty(lead, values):
            await lead.save()
        return lead
