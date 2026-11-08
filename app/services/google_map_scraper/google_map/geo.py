"""Map geometry for the scraper: city geocoding and the grid of search tiles."""

import http.client
import json
import math
import time
import urllib.parse
import urllib.request
from collections.abc import Mapping
from typing import Any, NotRequired, TypedDict

from app.utils.errors import ScraperError, describe

USER_AGENT = "Mozilla/5.0 (compatible; python_scraper_crawler-scraper/3.0)"

BoundingBox = tuple[float, float, float, float]  # (south, north, west, east)
LatLon = tuple[str, str]


class GeocodeResultWithBBox(TypedDict):
    lat: str
    lon: str
    boundingbox: list[str]


class GridCell(TypedDict):
    lat: str
    lon: str
    radius: int
    zoom: int
    _bbox: BoundingBox
    _sub: NotRequired[bool]


class GeocodingError(ScraperError):
    """Nominatim could not be asked, or its answer could not be read."""


def get_geocode(
    place: str,
    with_bbox: bool = False,
    country: str | None = None,
) -> LatLon | tuple[LatLon, list[str] | None] | None:
    """Resolve a place name to (lat, lon) via Nominatim.

    Returns the coordinate pair, or, when ``with_bbox`` is true, a tuple of
    the coordinate pair and the raw boundingbox (which may be ``None``).
    Returns ``None`` only when Nominatim answers with no match.

    :raises GeocodingError: When the request fails, the answer is not JSON,
        or the answer has no usable coordinates. The message names the
        failing step.
    """
    params: dict[str, str] = {"format": "json", "limit": "1", "q": place}
    if country:
        params["countrycodes"] = country.lower()

    query = urllib.parse.urlencode(params)
    url = f"https://nominatim.openstreetmap.org/search?{query}"
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})

    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            body = response.read()
    except (OSError, http.client.HTTPException) as error:
        raise GeocodingError(
            f"the request to Nominatim for {place!r} failed: {describe(error)}"
        ) from error
    time.sleep(1)  # respect Nominatim's ~1 req/sec policy
    try:
        hits: Any = json.loads(body)
    except ValueError as error:
        raise GeocodingError(
            f"Nominatim's answer for {place!r} is not JSON: {describe(error)}"
        ) from error
    if not isinstance(hits, list):
        raise GeocodingError(
            f"Nominatim's answer for {place!r} is a {type(hits).__name__}, not a list"
        )
    if not hits:
        return None
    try:
        first = hits[0]
        coords: LatLon = (str(first["lat"]), str(first["lon"]))
        boundingbox = first.get("boundingbox")
    except (KeyError, TypeError, AttributeError) as error:
        raise GeocodingError(
            f"Nominatim's first result for {place!r} has no usable lat/lon: "
            f"{describe(error)}"
        ) from error
    if with_bbox:
        return coords, boundingbox
    return coords


def build_grid_cells(bounding_box: BoundingBox, cells_per_side: int) -> list[GridCell]:
    """Split a bounding box into a ``cells_per_side`` x ``cells_per_side`` grid."""
    south, north, west, east = (float(x) for x in bounding_box)
    lat_step = (north - south) / cells_per_side
    lon_step = (east - west) / cells_per_side
    meters_per_degree = 6371000 * math.pi / 180

    cells: list[GridCell] = []
    for row in range(cells_per_side):
        for col in range(cells_per_side):
            center_lat = south + (row + 0.5) * lat_step
            center_lon = west + (col + 0.5) * lon_step
            cos_lat = math.cos(math.radians(center_lat))

            tile_width_m = max(1.0, lon_step * meters_per_degree * cos_lat)
            tile_diagonal_m = math.hypot(lat_step * meters_per_degree, tile_width_m)
            zoom = round(
                math.log2(156543.03 * cos_lat * 1000 / tile_width_m)
            )  # 156543 m/px at zoom 0

            cells.append(
                {
                    "lat": f"{center_lat:.6f}",
                    "lon": f"{center_lon:.6f}",
                    "radius": max(1000, math.ceil(1.15 * tile_diagonal_m / 2)),
                    "zoom": min(17, max(11, zoom)),
                    "_bbox": (
                        south + row * lat_step,
                        south + (row + 1) * lat_step,
                        west + col * lon_step,
                        west + (col + 1) * lon_step,
                    ),
                }
            )
    return cells


def is_in_bounding_box(
    row: Mapping[str, Any],
    bounding_box: BoundingBox,
    margin_ratio: float = 0.10,
) -> bool:
    """Return whether a row's coordinates fall within ``bounding_box`` (padded by margin)."""
    try:
        lat = float(row["latitude"])
        lon = float(row["longitude"])
    except (KeyError, TypeError, ValueError):
        return True

    south, north, west, east = (float(x) for x in bounding_box)
    lat_margin = (north - south) * margin_ratio
    lon_margin = (east - west) * margin_ratio
    return (
        south - lat_margin <= lat <= north + lat_margin
        and west - lon_margin <= lon <= east + lon_margin
    )
