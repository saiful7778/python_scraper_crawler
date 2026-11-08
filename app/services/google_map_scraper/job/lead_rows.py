"""Conversion of raw Maps leads into output rows (pure functions)."""

import math
import urllib.parse
from collections.abc import Mapping
from typing import Final

from ..extract_details import PlaceRecord, encode_plus_code
from ..google_map.parsing import PlaceLink
from .job_settings import JobSettings
from .job_types import LeadRow, SocialKey

COLUMNS: list[str] = [
    "input_id",
    "link",
    "title",
    "category",
    "address",
    "open_hours",
    "popular_times",
    "website",
    "phone",
    "plus_code",
    "review_count",
    "review_rating",
    "reviews_per_rating",
    "latitude",
    "longitude",
    "cid",
    "status",
    "descriptions",
    "reviews_link",
    "thumbnail",
    "timezone",
    "price_range",
    "data_id",
    "street_view_url",
    "place_id",
    "images",
    "reservations",
    "order_online",
    "menu",
    "owner",
    "complete_address",
    "credit_cards_accepted",
    "about",
    "user_reviews",
    "user_reviews_extended",
    "emails",
]

EXTRA: list[str] = ["categories"]

SOCIALS: Final[tuple[SocialKey, ...]] = (
    "instagram",
    "facebook",
    "linkedin",
    "tiktok",
    "youtube",
    "x",
)

_MAPS_SEARCH_URL = "https://www.google.com/maps/search/?api=1&query="


def result_columns(settings: JobSettings) -> list[str]:
    """Return the ordered output column names for a job's settings."""
    return (
        COLUMNS
        + EXTRA
        + (list(SOCIALS) if settings.collect_socials else [])
        + (["website_fetch_status"] if settings.use_browser_for_websites else [])
    )


def lead_identity_key(lead: Mapping[str, object]) -> str | None:
    """Return the value that identifies a lead, used to drop duplicates."""
    key = (
        lead.get("data_id")
        or lead.get("place_id")
        or lead.get("link")
        or lead.get("title")
    )
    return str(key) if key else None


def _sanitize_cell(value: object) -> str:
    """Stringify a value so it always encodes as UTF-8 and holds no NUL byte.

    A name cut in the middle of an emoji holds a lone surrogate, which UTF-8
    cannot encode; it becomes ``?``.
    """
    return str(value).replace("\x00", "").encode("utf-8", "replace").decode("utf-8")


def maps_place_url(title: str, place_id: str) -> str:
    """Return Google's documented Maps URL for one place."""
    return (
        _MAPS_SEARCH_URL
        + urllib.parse.quote(title, safe="")
        + "&query_place_id="
        + urllib.parse.quote(place_id, safe="")
    )


def _cell(source: Mapping[str, object], key: str) -> str:
    return _sanitize_cell(source[key]) if key in source else ""


def _coordinate(value: object) -> float | None:
    """A coordinate as a float, or ``None`` when it is blank or not a finite number."""
    if not isinstance(value, (int, float, str)):
        return None
    try:
        number = float(value)
    except ValueError:
        return None
    return number if math.isfinite(number) else None


def build_lead_row(lead: PlaceRecord | PlaceLink, keyword: str) -> LeadRow:
    """Build one output row from a raw lead found by ``keyword``."""
    source: Mapping[str, object] = lead
    title = _cell(source, "title")
    latitude = _coordinate(source.get("latitude"))
    longitude = _coordinate(source.get("longitude"))
    plus_code = _cell(source, "plus_code")
    if not plus_code:
        plus_code = encode_plus_code(latitude, longitude)
    link = _cell(source, "link")
    place_id = source.get("place_id")
    if place_id:
        link = maps_place_url(title, str(place_id))
    return LeadRow(
        input_id=keyword,
        link=link,
        title=title,
        category=_cell(source, "category"),
        address=_cell(source, "address"),
        open_hours=_cell(source, "open_hours"),
        popular_times=_cell(source, "popular_times"),
        website=_cell(source, "website"),
        phone=_cell(source, "phone"),
        plus_code=plus_code,
        review_count=_cell(source, "review_count"),
        review_rating=_cell(source, "review_rating"),
        reviews_per_rating=_cell(source, "reviews_per_rating"),
        latitude=latitude,
        longitude=longitude,
        cid=_cell(source, "cid"),
        status=_cell(source, "open_state"),
        descriptions=_cell(source, "description"),
        reviews_link=_cell(source, "reviews_link"),
        thumbnail=_cell(source, "thumbnail"),
        timezone=_cell(source, "timezone"),
        price_range=_cell(source, "price_range"),
        data_id=_cell(source, "data_id"),
        street_view_url=_cell(source, "street_view_url"),
        place_id=_cell(source, "place_id"),
        images=_cell(source, "images"),
        reservations=_cell(source, "reservations"),
        order_online=_cell(source, "order_online"),
        menu=_cell(source, "menu"),
        owner=_cell(source, "owner"),
        complete_address=_cell(source, "complete_address"),
        credit_cards_accepted=_cell(source, "credit_cards_accepted"),
        about=_cell(source, "about"),
        user_reviews=_cell(source, "user_reviews"),
        user_reviews_extended=_cell(source, "user_reviews_extended"),
        emails=_cell(source, "emails"),
        categories=_cell(source, "categories"),
    )
