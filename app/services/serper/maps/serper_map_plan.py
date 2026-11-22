"""Geocode the city and turn it into the pages one run fetches."""

import asyncio
from dataclasses import dataclass
from typing import cast

from app.repositories.serper_map_repository import SerperMapPage
from app.services.google_map_scraper.google_map.geo import (
    BoundingBox,
    LatLon,
    build_grid_cells,
    get_geocode,
)
from app.services.google_map_scraper.scraper import parse_bounding_box
from app.utils.errors import ScraperError, describe
from app.utils.logger import get_logger

from .serper_map_client import CREDITS_PER_PAGE
from .serper_map_config import SerperKeywordPlan, SerperMapConfig
from .serper_map_errors import SerperGeocodeError

logger = get_logger(__name__)

#: Zoom a whole-city search uses; a grid's zoom comes from its cell size.
WHOLE_CITY_ZOOM = 15


@dataclass(frozen=True)
class SerperMapPlan:
    """Everything one run will fetch, worked out before the first request."""

    config: SerperMapConfig
    center: LatLon
    bounding_box: BoundingBox | None
    pages: list[SerperMapPage]

    @property
    def tile_count(self) -> int:
        """Every keyword's own tiles, counted per keyword: two keywords at one tile are two."""
        return sum(
            len(
                {
                    (page.grid_x, page.grid_y)
                    for page in self.pages
                    if page.keyword == plan.keyword
                }
            )
            for plan in self.config.plans
        )

    @property
    def page_count(self) -> int:
        return len(self.pages)

    @property
    def estimated_credits(self) -> int:
        return CREDITS_PER_PAGE * len(self.pages)


async def build_plan(config: SerperMapConfig) -> SerperMapPlan:
    """Resolve the city and lay out one page request per keyword, tile and page."""
    found = await asyncio.to_thread(get_geocode, config.place, True, config.country)
    if not found:
        raise SerperGeocodeError(f"could not find coordinates for {config.place!r}")
    center, raw_box = cast(tuple[LatLon, list[str] | None], found)

    if not config.uses_grid:
        # The ll zoom is the search area, so a whole-city run uses the zoom the
        # city's own box would give a 1x1 grid, not a neighbourhood zoom.
        zoom = _whole_city_zoom(config, raw_box)
        return SerperMapPlan(
            config=config,
            center=center,
            bounding_box=None,
            pages=[
                page
                for plan in config.plans
                for page in _whole_city_pages(plan, center, zoom)
            ],
        )

    if not raw_box:
        raise SerperGeocodeError(
            f"{config.place!r} has no bounding box to split into a grid"
        )
    bounding_box = parse_bounding_box(config.place, raw_box)
    zoom = _whole_city_zoom(config, raw_box)
    return SerperMapPlan(
        config=config,
        center=center,
        bounding_box=bounding_box,
        pages=[
            page
            for plan in config.plans
            for page in (
                _whole_city_pages(plan, center, zoom)
                if plan.grid == 1
                else _grid_pages(plan, bounding_box)
            )
        ],
    )


def _whole_city_zoom(config: SerperMapConfig, raw_box: list[str] | None) -> int:
    """The zoom a whole-city search uses: its own box's 1x1 zoom, or the default."""
    zoom = WHOLE_CITY_ZOOM
    if raw_box:
        try:
            zoom = build_grid_cells(parse_bounding_box(config.place, raw_box), 1)[0][
                "zoom"
            ]
        except ScraperError as error:
            logger.warning(
                "the city box could not be read, using zoom %d: %s",
                WHOLE_CITY_ZOOM,
                describe(error),
            )
    return zoom


def _whole_city_pages(
    plan: SerperKeywordPlan, center: LatLon, zoom: int
) -> list[SerperMapPage]:
    lat, lon = center
    return [
        SerperMapPage(
            keyword=plan.keyword,
            page=page,
            grid_x=0,
            grid_y=0,
            lat=lat,
            lng=lon,
            zoom=zoom,
        )
        for page in range(1, plan.depth + 1)
    ]


def _grid_pages(plan: SerperKeywordPlan, box: BoundingBox) -> list[SerperMapPage]:
    """Every tile's every page, tile by tile so a shallow sweep covers the city first."""
    cells = build_grid_cells(box, plan.grid)
    pages: list[SerperMapPage] = []
    for index, cell in enumerate(cells):
        for page in range(1, plan.depth + 1):
            pages.append(
                SerperMapPage(
                    keyword=plan.keyword,
                    page=page,
                    grid_x=index % plan.grid,
                    grid_y=index // plan.grid,
                    lat=cell["lat"],
                    lng=cell["lon"],
                    zoom=cell["zoom"],
                )
            )
    logger.debug(
        "planned %d page(s) over %d tile(s) for %r",
        len(pages),
        len(cells),
        plan.keyword,
    )
    return pages
