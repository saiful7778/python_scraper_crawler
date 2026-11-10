"""Place-level scraping: one keyword in one city, as a single scrape or a grid of scrapes."""

from dataclasses import dataclass
from typing import cast

from app.services.browser import CDPError
from app.utils.errors import ScraperError, describe
from app.utils.logger import get_logger

from .google_map.geo import (
    BoundingBox,
    GridCell,
    LatLon,
    build_grid_cells,
    get_geocode,
    is_in_bounding_box,
)
from .place_query import PlaceQuery
from .scrape.dedupe import dedupe_rows
from .scrape.lead_rows import result_columns
from .scrape.scrape_runner import run_scrape
from .scrape.scrape_status import ScrapeStatus
from .scrape.scrape_types import LeadRow

logger = get_logger(__name__)

SATURATED = 20  # rows per scrape that indicates a tile hit Google's cap
SALVAGE: dict[str, list[LeadRow]] = {"rows": []}  # kept for Ctrl-C salvage


@dataclass
class ScrapeResult:
    """Rows that passed the filters, plus what happened while finding them."""

    rows: list[LeadRow]
    columns: list[str]
    finished: int
    skipped: int
    blocked: bool


def columns_for(query: PlaceQuery) -> list[str]:
    """The CSV columns a query produces."""
    return result_columns(query.engine_settings())


def _keep(row: LeadRow, query: PlaceQuery) -> bool:
    return (
        (not query.has_phone or bool(row.get("phone")))
        and (not query.has_website or bool(row.get("website")))
        and (not query.has_email or bool(row.get("emails")))
    )


def _finalize(
    rows: list[LeadRow], query: PlaceQuery, bounding_box: BoundingBox | None
) -> list[LeadRow]:
    kept = [
        row
        for row in rows
        if (bounding_box is None or is_in_bounding_box(row, bounding_box))
        and _keep(row, query)
    ]
    return kept[: query.limit] if query.limit else kept


class BoundingBoxError(ScraperError):
    """Nominatim's bounding box is not four numbers."""


def _parse_bounding_box(place: str, raw_box: list[str]) -> BoundingBox:
    """Turn Nominatim's ``[south, north, west, east]`` strings into floats.

    :raises BoundingBoxError: If there are not four values or one is not a number.
    """
    if len(raw_box) != 4:
        raise BoundingBoxError(
            f"the bounding box for {place!r} has {len(raw_box)} values, expected 4: {raw_box!r}"
        )
    try:
        south, north, west, east = (float(x) for x in raw_box)
    except (TypeError, ValueError) as error:
        raise BoundingBoxError(
            f"the bounding box for {place!r} has a value that is not a number: {raw_box!r}"
        ) from error
    return south, north, west, east


def _no_bounding_box(query: PlaceQuery) -> ScrapeResult:
    print("✗ Could not resolve a bounding box. Try a clearer --city or --state.")
    return ScrapeResult([], columns_for(query), 0, 0, False)


def scrape_place(
    query: PlaceQuery, known_keys: frozenset[str] = frozenset()
) -> ScrapeResult:
    settings = query.engine_settings()
    columns = result_columns(settings)
    SALVAGE["rows"] = []

    place = ", ".join(part for part in (query.city, query.state) if part)
    name = f"{query.keyword} @ {place}"[:80]  # the scrape name is what the web UI lists

    print(f'▶ Geocoding "{place}"')
    bounding_box: BoundingBox | None = None
    cells: list[GridCell] = []

    try:
        if query.grid:
            found = cast(
                tuple[LatLon, list[str] | None] | None,
                get_geocode(place, with_bbox=True, country=query.country),
            )
            if not found:
                return _no_bounding_box(query)
            (lat, lon), raw_box = found
            if not raw_box:
                return _no_bounding_box(query)
            bounding_box = _parse_bounding_box(place, raw_box)
            south, north, west, east = bounding_box
            cells = build_grid_cells(bounding_box, query.grid)
            print(f"\t→ bbox S{south} N{north} W{west} E{east}, {len(cells)} tiles")
        else:
            found_point = get_geocode(place, country=query.country)
            if not found_point:
                print("✗ Could not resolve coordinates. Try a clearer --city or --state.")
                return ScrapeResult([], columns, 0, 0, False)
            lat, lon = cast(LatLon, found_point)
            print(f"\t→ {lat}, {lon}")
    except ScraperError as error:
        print(f"✗ Could not geocode {place!r}: {describe(error)}")
        return ScrapeResult([], columns, 0, 1, False)

    anchored = settings.model_copy(
        update={"latitude": float(lat), "longitude": float(lon)}
    )

    if not cells:
        print(f"▶ Creating scrape: 1 keyword @ {lat},{lon} depth={query.scroll_depth}")
        result = run_scrape(name, anchored, known_keys=known_keys)
        if result.status == ScrapeStatus.FAILED:
            print(f"✗ {result.note}")
            return ScrapeResult([], columns, 0, 1, result.blocked)
        if result.note:
            print(f"  ⚠ {result.note}")
        rows = _finalize(dedupe_rows(result.rows, known_keys), query, None)
        return ScrapeResult(rows, columns, 1, 0, result.blocked)

    deep_scan_note = (
        " (! deep scan: a tile at Google's cap is split into 4 sub-tiles)"
        if query.deep_scan
        else ""
    )
    print(
        f"▶ Grid {query.grid}x{query.grid}: {len(cells)} scrapes, one at a time, "
        f"depth={query.scroll_depth}{deep_scan_note}"
    )

    rows: list[LeadRow] = []
    finished = skipped = 0
    blocked = False
    index = 0

    # Sequential on purpose: never run parallel scrapes against Google.
    while index < len(cells):
        cell = cells[index]
        index += 1
        sub_tag = " (sub-tile)" if cell.get("_sub") else ""
        print(
            f"▶ Cell {index}/{len(cells)} @ {cell['lat']},{cell['lon']} "
            f"radius={cell['radius']}m{sub_tag}"
        )
        cell_settings = anchored.model_copy(
            update={
                "latitude": float(cell["lat"]),
                "longitude": float(cell["lon"]),
                "zoom": cell["zoom"],
            }
        )

        try:
            result = run_scrape(name, cell_settings, known_keys=known_keys)
        except CDPError as exc:
            # The browser connection is gone; every later cell would fail the same way.
            print(
                f"\n  ✗ Stopped at cell {index}/{len(cells)}: Chrome connection lost "
                f"({describe(exc)})"
            )
            logger.exception("cell %d stopped on a lost Chrome connection", index)
            skipped += 1
            break
        except Exception as exc:
            print(f"\n  ⚠ cell {index}/{len(cells)} skipped: {describe(exc)}")
            logger.exception("cell %d skipped", index)
            skipped += 1
            continue

        rows = dedupe_rows(rows + result.rows, known_keys)
        if result.blocked:
            blocked = True
            print(
                f"\n  ⚠ Stopped at cell {index}/{len(cells)}: Google rate-limited "
                "this IP; the cells after it were not searched."
            )
            break
        if result.status == ScrapeStatus.FAILED:
            print(f"\n  ⚠ cell {index}/{len(cells)} skipped: {result.note}")
            skipped += 1
            continue

        finished += 1
        if result.note:
            print(f"  ⚠ cell {index}/{len(cells)}: {result.note}")
        kept = _finalize(rows, query, bounding_box)
        SALVAGE["rows"] = kept
        print(
            f"  cell {index}/{len(cells)}: {len(result.rows)} rows, "
            f"{len(kept)} kept so far"
        )
        if query.limit and len(kept) >= query.limit:
            print(f"  ✓ reached --limit {query.limit}; stopping")
            break

        if query.deep_scan and not cell.get("_sub") and len(result.rows) >= SATURATED:
            sub_cells: list[GridCell] = [
                {**cell, "_sub": True} for cell in build_grid_cells(cell["_bbox"], 2)
            ]
            cells.extend(sub_cells)  # one level only: sub-tiles never split again
            print(
                f"  ! cell {index} is at Google's cap: "
                f"{len(sub_cells)} sub-tiles added, {len(cells)} cells now"
            )

    kept = _finalize(rows, query, bounding_box)
    print(f"  {len(rows)} unique rows, {len(kept)} after the city box and filters")

    return ScrapeResult(kept, columns, finished, skipped, blocked)
