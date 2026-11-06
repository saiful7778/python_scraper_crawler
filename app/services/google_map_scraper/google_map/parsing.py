"""Pure functions and value types over Google Maps URLs and hrefs.

These have no dependency on a browser tab, so they can be unit-tested in
isolation and reused by both the feed collector and the single-place
fetch path.
"""

import urllib.parse
from typing import TypedDict

from .selectors import (
    CID_RE,
    END_MARKERS,
    LAT_LON_RE,
    PLACE_ID_RE,
)


class FeedEndState(TypedDict):
    """The shape returned by :data:`selectors.JS_FEED_END_STATE`."""

    cls: bool
    text: str


class PlaceLink(TypedDict):
    """One place extracted from a Maps href or feed anchor."""

    link: str
    title: str
    data_id: str
    latitude: str
    longitude: str
    place_id: str


class BatchStats(TypedDict):
    """Counters returned alongside collected leads."""

    batches: int
    dom_links: int


def build_maps_search_url(
    query: str,
    lat: float | None = None,
    lon: float | None = None,
    zoom: int = 15,
    lang: str = "en",
    country: str = "",
) -> str:
    """Build a Google Maps search URL.

    :param query: The search text; URL-encoded with no safe characters.
    :param lat: Optional latitude for the ``/@lat,lon,zoom`` viewport.
    :param lon: Optional longitude for the viewport.
    :param zoom: Viewport zoom level; ignored unless both lat and lon
        are given.
    :param lang: ``hl`` parameter (interface language).
    :param country: Optional ``gl`` parameter (country hint).
    :returns: The full search URL.
    """
    quoted = urllib.parse.quote(query, safe="")
    viewport = f"/@{lat},{lon},{zoom}z" if lat is not None and lon is not None else ""
    url = f"https://www.google.com/maps/search/{quoted}{viewport}?hl={lang}"
    if country:
        url += f"&gl={country}"
    return url


def feed_is_exhausted(feed_state: FeedEndState | None) -> bool:
    """Return whether the feed's end-of-list marker is present.

    :param feed_state: The value from ``JS_FEED_END_STATE``, or ``None``
        if the feed disappeared.
    :returns: True if the end-of-list class or any known end text is
        present.
    """
    if not feed_state:
        return False
    if feed_state.get("cls"):
        return True
    text = feed_state.get("text") or ""
    return any(marker in text for marker in END_MARKERS)


def parse_place_href(href: str, label: str = "") -> PlaceLink:
    """Extract a lead's fields from a Maps place href.

    :param href: A ``https://www.google.*/maps/place/...`` URL, or any
        Maps URL carrying ``!1s``/``!3d!4d``/``!19s`` markers.
    :param label: Optional human-readable label (e.g. the anchor's
        ``aria-label``); used as the title when the path has none.
    :returns: A dict with link, title, data_id, latitude, longitude, and
        place_id. Missing fields are empty strings.
    """
    path = urllib.parse.urlparse(href).path
    name = ""
    if "/maps/place/" in path:
        name = urllib.parse.unquote_plus(
            path.split("/maps/place/", 1)[1].split("/", 1)[0]
        )
    cid_match = CID_RE.search(href)
    latlon_match = LAT_LON_RE.search(href)
    place_match = PLACE_ID_RE.search(href)
    return {
        "link": href,
        "title": label or name,
        "data_id": cid_match.group(1) if cid_match else "",
        "latitude": latlon_match.group(1) if latlon_match else "",
        "longitude": latlon_match.group(2) if latlon_match else "",
        "place_id": place_match.group(1) if place_match else "",
    }
