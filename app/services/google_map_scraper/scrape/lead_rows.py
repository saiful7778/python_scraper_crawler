"""Conversion of raw Maps leads into output rows (pure functions)."""

import json
import math
import urllib.parse
from collections.abc import Mapping

from app.constant import SOCIALS
from app.services.crawler.extractors.tech import TECH_FIELDS

from ..extract_details import (
    PlaceRecord,
    address_json,
    encode_plus_code,
    parse_address_json,
)
from ..google_map.parsing import PlaceLink
from .scrape_settings import ScrapeSettings
from .scrape_types import LeadRow

COLUMNS: list[str] = [
    "input_id",
    "link",
    "title",
    "categories",
    "address",
    "open_hours",
    "popular_times",
    "website",
    "phone",
    "plus_code",
    "review_count",
    "review_rating",
    "reviews_per_rating",
    "review_star_1_count",
    "review_star_2_count",
    "review_star_3_count",
    "review_star_4_count",
    "review_star_5_count",
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
]

_MAPS_SEARCH_URL = "https://www.google.com/maps/search/?api=1&query="


def result_columns(settings: ScrapeSettings) -> list[str]:
    """Return the ordered output column names for a scrape's settings."""
    return (
        COLUMNS
        + (list(SOCIALS) if settings.collect_socials else [])
        + TECH_FIELDS
    )


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


def cell_text(value: object) -> str:
    """The text of a row cell: a list or dict as JSON, anything else as text."""
    if isinstance(value, (list, dict)):
        value = json.dumps(value, ensure_ascii=False)
    return _sanitize_cell(value)


def _cell(source: Mapping[str, object], key: str) -> str:
    return cell_text(source[key]) if key in source else ""


def _list_cell(source: Mapping[str, object], key: str) -> str:
    """A list cell as its items joined by " | ", or "" when the source has no list."""
    items = source.get(key)
    return (
        " | ".join(_sanitize_cell(item) for item in items)
        if isinstance(items, list)
        else ""
    )


def review_start_columns(histogram: str) -> dict[str, str]:
    """Split a ``reviews_per_rating`` JSON histogram into one count per star rating."""
    try:
        counts = json.loads(histogram) if histogram else {}
    except ValueError:
        counts = {}
    if not isinstance(counts, dict):
        counts = {}
    return {
        f"review_star_{star}_count": (
            _sanitize_cell(counts[str(star)]) if counts.get(str(star)) is not None else ""
        )
        for star in range(1, 6)
    }


def _coordinate(value: object) -> float | None:
    """A coordinate as a float, or ``None`` when it is blank or not a finite number."""
    if not isinstance(value, (int, float, str)):
        return None
    try:
        number = float(value)
    except ValueError:
        return None
    return number if math.isfinite(number) else None


def address_fields(row: LeadRow) -> dict[str, object]:
    """The columns of an address record for a row, with blank values left out."""
    parts: dict[str, object] = dict(
        parse_address_json(row.get("complete_address", "")) or {}
    )
    fields: dict[str, object] = {
        **parts,
        "complete_address": row.get("address", ""),
        "plus_code": row.get("plus_code", ""),
        "latitude": row.get("latitude"),
        "longitude": row.get("longitude"),
    }
    return {k: v for k, v in fields.items() if v not in (None, "")}


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
        address=_cell(source, "address"),
        open_hours=_cell(source, "open_hours"),
        popular_times=_cell(source, "popular_times"),
        website=_cell(source, "website"),
        phone=_cell(source, "phone"),
        plus_code=plus_code,
        review_count=_cell(source, "review_count"),
        review_rating=_cell(source, "review_rating"),
        reviews_per_rating=_cell(source, "reviews_per_rating"),
        **review_start_columns(_cell(source, "reviews_per_rating")),
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
        complete_address=address_json(source.get("complete_address")),
        credit_cards_accepted=_cell(source, "credit_cards_accepted"),
        about=_cell(source, "about"),
        user_reviews=_cell(source, "user_reviews"),
        user_reviews_extended=_cell(source, "user_reviews_extended"),
        categories=_list_cell(source, "categories"),
    )
