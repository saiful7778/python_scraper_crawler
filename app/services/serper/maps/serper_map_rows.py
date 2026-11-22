"""Turn a typed Serper place into the lead row the existing tables and dedupe expect."""

import dataclasses
import json

from app.services.google_map_scraper.extract_details import encode_plus_code
from app.services.google_map_scraper.scrape.lead_filter import keep_lead
from app.services.google_map_scraper.scrape.lead_rows import maps_place_url
from app.services.google_map_scraper.scrape.phone_numbers import format_phone_numbers
from app.services.google_map_scraper.scrape.scrape_settings import ScrapeSettings
from app.services.google_map_scraper.scrape.scrape_types import LeadRow

from .serper_map_config import SerperMapConfig
from .serper_map_type import Place

__all__ = ["place_to_row", "places_to_rows"]


def _text(value: object) -> str:
    """A cell's text: ``""`` for nothing, JSON for structures, text otherwise."""
    if value is None:
        return ""
    if isinstance(value, (list, dict)):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def _hours(place: Place) -> str:
    """The opening hours as ``{day: [span, ...]}``, the shape the hours table reads.

    Serper gives one string per day, with several ranges separated by commas.
    """
    if place.openingHours is None:
        return ""
    spans: dict[str, list[str]] = {}
    for day, text in dataclasses.asdict(place.openingHours).items():
        if not text:
            continue
        parts = [part.strip() for part in str(text).split(", ") if part.strip()]
        if parts:
            spans[day] = parts
    return json.dumps(spans, ensure_ascii=False) if spans else ""


def place_to_row(place: Place, keyword: str) -> LeadRow:
    """Build one lead row from a Serper place found by ``keyword``.

    The dedupe keys are preserved: ``place_id`` from ``placeId``, ``data_id`` from
    ``fid`` (the ``0x…:0x…`` shape the parser turns into a CID) and ``title``.
    """
    title = _text(place.title)
    place_id = _text(place.placeId)
    link = maps_place_url(title, place_id) if place_id else ""
    return LeadRow(
        input_id=keyword,
        link=link,
        title=title,
        categories=" | ".join(_text(item) for item in place.types),
        address=_text(place.address),
        open_hours=_hours(place),
        popular_times="",
        website=_text(place.website),
        phone=_text(place.phoneNumber),
        plus_code=encode_plus_code(place.latitude, place.longitude),
        review_count=_text(place.ratingCount),
        review_rating=_text(place.rating),
        reviews_per_rating="",
        review_star_1_count="",
        review_star_2_count="",
        review_star_3_count="",
        review_star_4_count="",
        review_star_5_count="",
        latitude=place.latitude,
        longitude=place.longitude,
        cid=_text(place.cid),
        descriptions=_text(place.type),
        reviews_link="",
        thumbnail=_text(place.thumbnailUrl),
        timezone="",
        price_range="",
        data_id=_text(place.fid),
        street_view_url="",
        place_id=place_id,
        images="",
        reservations="",
        order_online="",
        menu="",
        owner="",
        complete_address="",
        credit_cards_accepted="",
        about="",
        user_reviews="",
    )


def places_to_rows(
    places: list[Place], keyword: str, config: SerperMapConfig
) -> list[LeadRow]:
    """Map every place, then drop the ones the phone or website filter refuses.

    The phone is formatted for the country first, so a number that cannot be
    parsed is gone before ``--has-phone`` counts it.
    """
    rows = [place_to_row(place, keyword) for place in places]
    format_phone_numbers(rows, config.country)
    settings = ScrapeSettings(
        keywords=(config.keywords[0],),
        has_phone=config.has_phone,
        has_website=config.has_website,
    )
    return [row for row in rows if keep_lead(row, settings)]
