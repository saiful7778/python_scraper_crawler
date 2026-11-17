import hashlib
import json
from dataclasses import dataclass
from typing import cast
from uuid import UUID

from tortoise.transactions import in_transaction

from app.constant import SOCIALS
from app.models import (
    GoogleMapKeyword,
    GoogleMapProfileHourModel,
    GoogleMapProfileImageModel,
    GoogleMapProfileModel,
    GoogleMapProfileReviewModel,
    KeywordModel,
    LeadAddressModel,
    LeadCategoryModel,
    LeadModel,
    LeadSocialModel,
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


#: Rows per bulk insert; matches the flusher's batch size.
BULK_SIZE = 500


@dataclass
class _PreparedRow:
    """One business of a batch: its values, the stored profile it matched, or the lead it will get."""

    row: LeadRow
    profile: GoogleMapProfileModel | None = None
    lead: LeadModel | None = None


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
        """Save a tile's rows, writing each table in bulk, and return how many were saved.

        The batch is one transaction: the stored profiles it matches are read in a
        few queries, the new leads and profiles are inserted in one statement per
        table, and rows that describe the same business are merged first, the way
        saving them one after another would fill the same blanks. A failure rolls
        the batch back and the rows are saved one by one instead, so a bad row
        still stops the batch and the rows before it are kept.
        """
        try:
            return await self._save_batch(rows, scrape_id)
        except CONNECTION_LOST:
            raise
        except Exception as error:
            logger.error(
                "the bulk save of %d row(s) of scrape %s failed, saving them one by one: %s",
                len(rows),
                scrape_id,
                describe(error),
                exc_info=error,
            )
        return await self._save_one_by_one(rows, scrape_id)

    async def _save_batch(self, rows: list[LeadRow], scrape_id: str) -> int:
        async with in_transaction():
            prepared = await self._prepare_batch(rows)
            await self._write_batch(prepared, scrape_id)
        return len(rows)

    async def _save_one_by_one(self, rows: list[LeadRow], scrape_id: str) -> int:
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

    async def _prepare_batch(self, rows: list[LeadRow]) -> list[_PreparedRow]:
        """Match each row to a stored profile, merging rows that describe one business."""
        (
            stored_by_place,
            stored_by_place_cid,
            stored_by_title,
        ) = await self._stored_matches(rows)
        prepared: list[_PreparedRow] = []
        by_place: dict[str, _PreparedRow] = {}
        by_place_cid: dict[tuple[str, str], _PreparedRow] = {}
        by_title: dict[tuple[str, str | None], _PreparedRow] = {}

        for row in rows:
            place_id = (row.get("place_id") or "").strip()
            cid = (row.get("cid") or "").strip()
            title = (row.get("title") or "").strip()
            domain = website_domain(row.get("website", ""))

            entry: _PreparedRow | None = None
            profile: GoogleMapProfileModel | None = None
            if place_id:
                entry = (
                    by_place_cid.get((place_id, cid)) if cid else by_place.get(place_id)
                )
                if entry is None:
                    profile = (
                        stored_by_place_cid.get((place_id, cid))
                        if cid
                        else stored_by_place.get(place_id)
                    )
            elif title:
                key = (title, domain)
                entry = by_title.get(key)
                if entry is None:
                    profile = stored_by_title.get(key)

            if entry is None:
                entry = _PreparedRow(dict(row), profile)
                prepared.append(entry)
            else:
                self._merge_row(entry.row, row)

            if place_id:
                by_place.setdefault(place_id, entry)
                if cid:
                    by_place_cid.setdefault((place_id, cid), entry)
            elif title:
                by_title.setdefault((title, domain), entry)

        return prepared

    async def _stored_matches(self, rows: list[LeadRow]):
        """The stored profiles a batch may match, looked up once for the whole batch."""
        place_ids = {
            (row.get("place_id") or "").strip()
            for row in rows
            if (row.get("place_id") or "").strip()
        }
        domains = {
            domain for row in rows if (domain := website_domain(row.get("website", "")))
        }
        titles = {
            (row.get("title") or "").strip()
            for row in rows
            if not (row.get("place_id") or "").strip()
            and (row.get("title") or "").strip()
        }

        by_place: dict[str, GoogleMapProfileModel] = {}
        by_place_cid: dict[tuple[str, str], GoogleMapProfileModel] = {}
        if place_ids:
            for profile in await self._model.filter(
                place_id__in=list(place_ids)
            ).select_related("lead"):
                by_place.setdefault(profile.place_id, profile)
                if profile.cid:
                    by_place_cid.setdefault((profile.place_id, profile.cid), profile)

        by_title: dict[tuple[str, str | None], GoogleMapProfileModel] = {}
        leads = []
        if domains:
            leads += await LeadModel.filter(website_domain__in=list(domains))
        if titles:
            leads += await LeadModel.filter(
                website_domain__isnull=True, title__in=list(titles)
            )
        if leads:
            for profile in await self._model.filter(
                lead_id__in=[lead.id for lead in leads]
            ).select_related("lead"):
                lead = profile.lead
                if lead.title:
                    by_title.setdefault((lead.title, lead.website_domain), profile)

        return by_place, by_place_cid, by_title

    @staticmethod
    def _merge_row(into: LeadRow, row: LeadRow) -> None:
        """Fill ``into``'s blanks from ``row``, as a second save of the same business would."""
        for key, value in row.items():
            if key == "is_crawlable":
                if value is not None:
                    into[key] = value
            elif into.get(key) in (None, "") and value not in (None, ""):
                into[key] = value

    async def _write_batch(self, prepared: list[_PreparedRow], scrape_id: str) -> None:
        new = [entry for entry in prepared if entry.profile is None]
        if new:
            await self._insert_new(new)
        for entry in prepared:
            if entry.lead is None:
                await self._update_stored(entry.row, entry.profile)
        await self._save_children(
            [(entry.row, entry.profile) for entry in prepared], scrape_id
        )

    async def _insert_new(self, entries: list[_PreparedRow]) -> None:
        """Insert the batch's new leads, their profiles and child rows, one statement per table."""
        wanted = {
            domain
            for entry in entries
            if (domain := website_domain(entry.row.get("website", "")))
        }
        used: set[str] = set()
        if wanted:
            used = set(
                await LeadModel.filter(website_domain__in=list(wanted)).values_list(
                    "website_domain", flat=True
                )
            )

        leads: list[LeadModel] = []
        for entry in entries:
            row = entry.row
            values = {name: row.get(name) or None for name in LEAD_FIELDS}
            domain = website_domain(row.get("website", ""))
            if domain and domain not in used:
                used.add(domain)
            else:
                domain = None
            crawlable = row.get("is_crawlable")
            lead = LeadModel(
                counter=await self._leads._next_global_counter(),
                website_domain=domain,
                is_crawlable=bool(crawlable) if crawlable is not None else False,
                **values,
            )
            entry.lead = lead
            leads.append(lead)
        await LeadModel.bulk_create(leads, batch_size=BULK_SIZE)

        profiles: list[GoogleMapProfileModel] = []
        for entry in entries:
            profile = GoogleMapProfileModel(
                counter=await self._next_global_counter(),
                lead_id=entry.lead.id,
                **_profile_values(entry.row),
            )
            entry.profile = profile
            profiles.append(profile)
        await GoogleMapProfileModel.bulk_create(profiles, batch_size=BULK_SIZE)

        await self._insert_new_children(entries)

    async def _insert_new_children(self, entries: list[_PreparedRow]) -> None:
        addresses: list[LeadAddressModel] = []
        socials: list[LeadSocialModel] = []
        categories_by_entry: list[tuple[_PreparedRow, list[str]]] = []
        for entry in entries:
            assert entry.lead is not None
            parts = _children(entry.row)
            if parts["address"]:
                addresses.append(
                    LeadAddressModel(lead_id=entry.lead.id, **parts["address"])
                )
            socials += [
                LeadSocialModel(lead_id=entry.lead.id, platform=platform, url=url)
                for platform, url in parts["socials"].items()
            ]
            categories_by_entry.append(
                (entry, list(dict.fromkeys(parts["categories"])))
            )

        if addresses:
            await LeadAddressModel.bulk_create(addresses, batch_size=BULK_SIZE)
        if socials:
            await LeadSocialModel.bulk_create(
                socials, ignore_conflicts=True, batch_size=BULK_SIZE
            )
        names = list(
            dict.fromkeys(
                name for _, row_names in categories_by_entry for name in row_names
            )
        )
        if names:
            await LeadCategoryModel.bulk_create(
                [LeadCategoryModel(name=name) for name in names],
                ignore_conflicts=True,
                batch_size=BULK_SIZE,
            )
            stored = {
                category.name: category
                for category in await LeadCategoryModel.filter(name__in=names)
            }
            for entry, row_names in categories_by_entry:
                if row_names:
                    assert entry.lead is not None
                    await entry.lead.categories.add(
                        *[stored[name] for name in row_names]
                    )

    async def _update_stored(
        self, row: LeadRow, profile: GoogleMapProfileModel
    ) -> None:
        """Fill a stored profile and its lead from a row, never overwriting a stored value."""
        lead = profile.lead
        values = {name: row.get(name) or None for name in LEAD_FIELDS}
        crawlable = row.get("is_crawlable")
        changed = self.fill_empty(lead, values)
        if crawlable is not None and lead.is_crawlable != bool(crawlable):
            lead.is_crawlable = bool(crawlable)
            changed = True
        if changed:
            await lead.save()
        await self._leads.add_children(lead, _children(row))
        self.fill_empty(profile, _profile_values(row))
        profile.lead = lead
        await profile.save()

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
        crawlable = row.get("is_crawlable")
        lead = await self._linked_lead(profile)
        if lead is None:
            domain = await self._leads.find_by_domain(
                website_domain(row.get("website", ""))
            )
            return await self._leads.create(
                {
                    **values,
                    "website_domain": domain,
                    **(
                        {"is_crawlable": bool(crawlable)}
                        if crawlable is not None
                        else {}
                    ),
                }
            )
        changed = self.fill_empty(lead, values)
        if crawlable is not None and lead.is_crawlable != bool(crawlable):
            lead.is_crawlable = bool(crawlable)
            changed = True
        if changed:
            await lead.save()
        return lead
