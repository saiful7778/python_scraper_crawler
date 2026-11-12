"""Scrapes the keywords in one city, as a single scrape or a grid of scrapes, and stores the leads."""

import time
from typing import cast

from redis import Redis
from redis.exceptions import RedisError

from app.repositories import GoogleMapProfileRepository, LeadRepository
from app.services.browser import CDPError
from app.services.buffer.client import BufferError, connect
from app.services.buffer.discard import discard_scrape
from app.services.buffer.flusher import Flusher
from app.services.buffer.row_buffer import RowBuffer
from app.services.buffer.scrape_state import ScrapeState
from app.services.buffer.website_queue import WebsiteQueue
from app.utils.errors import ScraperError, describe
from app.utils.logger import get_logger
from config.redis import RedisConfig
from config.tortoise import TortoiseConfig

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
from .scrape.engine_config import EngineConfig
from .scrape.lead_rows import result_columns
from .scrape.scrape_database import ScrapeDatabase
from .scrape.scrape_limits import BUFFER_TTL_SECONDS, MAX_RESUMES
from .scrape.scrape_run import RunOutcome
from .scrape.scrape_settings import ScrapeSettings
from .scrape.scrape_status import ScrapeStatus
from .scrape.scrape_types import LeadRow, RunResult, ScrapeResult, SettingsData
from .scrape.scrape_worker import ScrapeWorker

logger = get_logger(__name__)

SATURATED = 20  # rows per scrape that indicates a tile hit Google's cap

_POLL_SECONDS = 1
#: Seconds the flusher gets for its last flush; a slow PostgreSQL may need long.
_FINAL_FLUSH_SECONDS = 60


class BoundingBoxError(ScraperError):
    """Nominatim's bounding box is not four numbers."""


class ScrapeDeletedError(ScraperError):
    """The scrape was deleted while it was running, so it has no result."""


class ScrapeWorkerDiedError(ScraperError):
    """The scrape worker thread stopped before the scrape reached an outcome."""


def columns_for(query: PlaceQuery) -> list[str]:
    """The CSV columns a query produces."""
    return result_columns(query.engine_settings())


def _has_value(row: LeadRow, column: str) -> bool:
    return bool(str(row.get(column) or "").strip())


def keep_row(row: LeadRow, query: PlaceQuery) -> bool:
    return (not query.has_phone or _has_value(row, "phone")) and (
        not query.has_website or _has_value(row, "website")
    )


def finalize_rows(
    rows: list[LeadRow], query: PlaceQuery, bounding_box: BoundingBox | None
) -> list[LeadRow]:
    kept = [
        row
        for row in rows
        if (bounding_box is None or is_in_bounding_box(row, bounding_box))
        and keep_row(row, query)
    ]
    return kept[: query.limit] if query.limit else kept


def parse_bounding_box(place: str, raw_box: list[str]) -> BoundingBox:
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
    logger.debug("bounding box of %r: S%s N%s W%s E%s", place, south, north, west, east)
    return south, north, west, east


def _no_bounding_box(query: PlaceQuery) -> ScrapeResult:
    logger.warning("could not resolve a bounding box for the city")
    print("✗ Could not resolve a bounding box. Try a clearer --city or --state.")
    return ScrapeResult([], columns_for(query), 0, 0, False)


class ScrapeSession:
    """The threads and Redis buffers of one ``run_scrape`` call."""

    def __init__(
        self,
        config: EngineConfig,
        redis: Redis,
        postgres: TortoiseConfig,
        known_keys: frozenset[str],
    ):
        self._redis = redis
        self._state = ScrapeState(redis, BUFFER_TTL_SECONDS)
        self._rows = RowBuffer(redis, BUFFER_TTL_SECONDS)
        self._queue = WebsiteQueue(redis, BUFFER_TTL_SECONDS)
        self._finished: dict[str, tuple[RunOutcome, list[LeadRow]]] = {}
        self._deleted: set[str] = set()
        self._database = ScrapeDatabase(postgres)
        self._worker = ScrapeWorker(
            self._database,
            redis,
            headless=config.headless,
            known_keys=known_keys,
            on_finished=self._keep_finished,
        )
        self._flusher = Flusher(
            self._rows,
            self._state,
            save_row=GoogleMapProfileRepository(LeadRepository()).save_row,
            crawl_sink=None,
            config=postgres,
        )

    def run(self, name: str, settings: ScrapeSettings) -> RunResult:
        carried = self._resume_interrupted()
        scrape_id = self._database.create(
            name, cast(SettingsData, settings.model_dump(by_alias=True, mode="json"))
        )
        self._flusher.start()
        self._worker.start()
        for earlier in carried:
            try:
                self._wait_for_scrape(earlier)
            except ScrapeDeletedError:
                logger.info("scrape %s was deleted while it ran", earlier)
                self._drop(earlier)
        try:
            self._wait_for_scrape(scrape_id)
        except ScrapeDeletedError:
            self._drop(scrape_id)
            raise
        return self._finish(carried, scrape_id)

    def stop(self) -> None:
        for name, stop in (
            ("scrape worker", lambda: self._worker.shutdown(wait=1)),
            ("flusher", lambda: self._flusher.shutdown(wait=_FINAL_FLUSH_SECONDS)),
            ("scrape database", self._database.close),
        ):
            try:
                stop()
            except Exception:
                logger.exception("stopping the %s failed", name)
        for scrape_id in self._deleted:
            try:
                discard_scrape(self._redis, scrape_id)
            except Exception:
                logger.exception("discarding the deleted scrape %s failed", scrape_id)

    def _resume_interrupted(self) -> list[str]:
        resumed, failed = self._database.requeue_interrupted(MAX_RESUMES)
        if resumed or failed:
            logger.warning(
                "%s interrupted scrape(s) resume, %s failed after %s resumes",
                resumed,
                failed,
                MAX_RESUMES,
            )
        carried = self._database.pending_ids()
        for scrape_id in carried:
            self._queue.requeue_processing(scrape_id)
        return carried

    def _keep_finished(
        self, scrape_id: str, outcome: RunOutcome, rows: list[LeadRow]
    ) -> None:
        self._finished[scrape_id] = (outcome, rows)

    def _check_exists(self, scrape_id: str) -> None:
        if self._database.get(scrape_id) is None:
            self._deleted.add(scrape_id)
            self._worker.cancel(scrape_id)
            raise ScrapeDeletedError(f"scrape {scrape_id} was deleted while it ran")

    def _wait_for_scrape(self, scrape_id: str) -> None:
        while scrape_id not in self._finished:
            self._check_exists(scrape_id)
            if not self._worker.is_alive():
                if self._worker.failure is not None:
                    raise self._worker.failure
                raise ScrapeWorkerDiedError(
                    f"the scrape worker stopped while scrape {scrape_id} had no outcome"
                )
            time.sleep(_POLL_SECONDS)

    def _drop(self, scrape_id: str) -> None:
        discard_scrape(self._redis, scrape_id)
        self._finished.pop(scrape_id, None)

    def _finish(self, carried: list[str], scrape_id: str) -> RunResult:
        ids = [i for i in (*carried, scrape_id) if i in self._finished]
        self._worker.shutdown(wait=1)
        self._flusher.shutdown(wait=_FINAL_FLUSH_SECONDS)
        result: RunResult | None = None
        for finished_id in ids:
            outcome, rows = self._finished[finished_id]
            progress = self._state.get_progress(finished_id)
            left = self._rows.length(finished_id)
            if left:
                logger.warning(
                    "PostgreSQL did not take %d buffered lead(s) of scrape %s; they stay in Redis and the scrape resumes on the next start",
                    left,
                    finished_id,
                )
                if finished_id == scrape_id:
                    unsaved = (
                        f"{left} lead(s) not saved to PostgreSQL; they stay in Redis "
                        "and the scrape resumes on the next start"
                    )
                    result = RunResult(
                        scrape_id=scrape_id,
                        status=outcome.status,
                        note="; ".join(filter(None, (outcome.note, unsaved))),
                        rows=rows,
                        blocked=bool(progress.get("blocked")),
                    )
                continue
            discard_scrape(self._redis, finished_id)
            if not self._database.finish(
                finished_id, outcome.status, outcome.note, progress
            ):
                if finished_id == scrape_id:
                    raise ScrapeDeletedError(
                        f"scrape {scrape_id} was deleted while it ran"
                    )
                continue
            if finished_id == scrape_id:
                result = RunResult(
                    scrape_id=scrape_id,
                    status=outcome.status,
                    note=outcome.note,
                    rows=rows,
                    blocked=bool(progress.get("blocked")),
                )
        if result is None:
            raise ScrapeWorkerDiedError(f"scrape {scrape_id} ended without an outcome")
        return result


def run_scrape(
    name: str,
    settings: ScrapeSettings,
    *,
    config: EngineConfig | None = None,
    known_keys: frozenset[str] = frozenset(),
    redis: Redis | None = None,
    postgres: TortoiseConfig | None = None,
) -> RunResult:
    """Store the scrape, run it on Redis and PostgreSQL and wait until it has an outcome.

    Scrapes an earlier run left unfinished resume first. Then the scrape worker
    runs this scrape while the flusher saves its leads. Leads whose keys are in
    ``known_keys`` are skipped. The Redis data of the scrape is deleted when it
    ends, and its ``scrapes`` row stays with the final status.

    :raises ScrapeDeletedError: If the scrape is deleted while it runs.
    :raises ScrapeWorkerDiedError: If the worker thread stops before the scrape
        has an outcome, so the wait would otherwise never end.
    :raises BufferError: If Redis fails.
    :raises ScrapeDatabaseError: If PostgreSQL fails.
    """
    logger.debug("starting scrape %r with %d known keys", name, len(known_keys))
    session = ScrapeSession(
        config or EngineConfig(),
        redis or connect(RedisConfig()),
        postgres or TortoiseConfig(),
        known_keys,
    )
    try:
        result = session.run(name, settings)
        logger.info(
            "scrape %s ended as %s with %d rows",
            result.scrape_id,
            result.status,
            len(result.rows),
        )
        return result
    except RedisError as error:
        raise BufferError(
            f"Redis failed while running the scrape: {describe(error)}"
        ) from error
    finally:
        session.stop()


class PlaceScraper:
    """Scrapes the keywords in one city, as a single scrape or a grid of scrapes."""

    def __init__(self) -> None:
        self._salvage: list[LeadRow] = []

    @property
    def salvage_rows(self) -> list[LeadRow]:
        """The leads kept so far, so a Ctrl-C can still save them."""
        return self._salvage

    def scrape(
        self, query: PlaceQuery, known_keys: frozenset[str] = frozenset()
    ) -> ScrapeResult:
        settings = query.engine_settings()
        columns = result_columns(settings)
        self._salvage = []

        place = ", ".join(part for part in (query.city, query.state) if part)
        name = f"{', '.join(query.keywords)} @ {place}"[
            :80
        ]  # the scrape name is what the web UI lists

        logger.info(
            "scraping %r in %r: grid=%s depth=%s",
            query.keywords,
            place,
            query.grid,
            query.scroll_depth,
        )
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
                bounding_box = parse_bounding_box(place, raw_box)
                south, north, west, east = bounding_box
                cells = build_grid_cells(bounding_box, query.grid)
                print(f"\t→ bbox S{south} N{north} W{west} E{east}, {len(cells)} tiles")
                logger.info("geocoded %r: %d grid tiles", place, len(cells))
            else:
                found_point = get_geocode(place, country=query.country)
                if not found_point:
                    logger.warning("Nominatim found no coordinates for %r", place)
                    print(
                        "✗ Could not resolve coordinates. Try a clearer --city or --state."
                    )
                    return ScrapeResult([], columns, 0, 0, False)
                lat, lon = cast(LatLon, found_point)
                print(f"\t→ {lat}, {lon}")
                logger.info("geocoded %r: %s, %s", place, lat, lon)
        except ScraperError as error:
            logger.error("geocoding %r failed: %s", place, describe(error))
            print(f"✗ Could not geocode {place!r}: {describe(error)}")
            return ScrapeResult([], columns, 0, 1, False)

        anchored = settings.model_copy(
            update={"latitude": float(lat), "longitude": float(lon)}
        )

        if not cells:
            print(
                f"▶ Creating scrape: 1 keyword @ {lat},{lon} depth={query.scroll_depth}"
            )
            logger.info("single scrape at %s,%s", lat, lon)
            result = run_scrape(name, anchored, known_keys=known_keys)
            if result.status == ScrapeStatus.FAILED:
                logger.warning("single scrape failed: %s", result.note)
                print(f"✗ {result.note}")
                return ScrapeResult([], columns, 0, 1, result.blocked)
            if result.note:
                logger.info("single scrape note: %s", result.note)
                print(f"⚠ {result.note}")
            rows = finalize_rows(dedupe_rows(result.rows, known_keys), query, None)
            logger.info(
                "single scrape: %d rows, %d kept after dedupe and filters",
                len(result.rows),
                len(rows),
            )
            return ScrapeResult(rows, columns, 1, 0, result.blocked)

        deep_scan_note = (
            " (! deep scan: a tile at Google's cap is split into 4 sub-tiles)"
            if query.deep_scan
            else ""
        )
        print(
            f"▶ Grid {query.grid}x{query.grid}: {len(cells)} scrapes, one at a time, depth={query.scroll_depth}{deep_scan_note}"
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
                f"▶ Cell {index}/{len(cells)} @ {cell['lat']},{cell['lon']} radius={cell['radius']}m{sub_tag}"
            )
            cell_settings = anchored.model_copy(
                update={
                    "latitude": float(cell["lat"]),
                    "longitude": float(cell["lon"]),
                    "zoom": cell["zoom"],
                }
            )

            logger.debug(
                "cell %d/%d: lat=%s lon=%s zoom=%s radius=%sm",
                index,
                len(cells),
                cell["lat"],
                cell["lon"],
                cell["zoom"],
                cell["radius"],
            )
            try:
                result = run_scrape(name, cell_settings, known_keys=known_keys)
            except CDPError as exc:
                # The browser connection is gone; every later cell would fail the same way.
                print(
                    f"\n✗ Stopped at cell {index}/{len(cells)}: Chrome connection lost ({describe(exc)})"
                )
                logger.exception("cell %d stopped on a lost Chrome connection", index)
                skipped += 1
                break
            except Exception as exc:
                print(f"\n⚠ cell {index}/{len(cells)} skipped: {describe(exc)}")
                logger.exception("cell %d skipped", index)
                skipped += 1
                continue

            rows = dedupe_rows(rows + result.rows, known_keys)
            if result.blocked:
                blocked = True
                logger.warning(
                    "rate-limited at cell %d; the cells after it are not searched",
                    index,
                )
                print(
                    f"\n⚠ Stopped at cell {index}/{len(cells)}: Google rate-limited this IP; the cells after it were not searched."
                )
                break
            if result.status == ScrapeStatus.FAILED:
                print(f"\n  ⚠ cell {index}/{len(cells)} skipped: {result.note}")
                skipped += 1
                continue

            finished += 1
            if result.note:
                print(f"⚠ cell {index}/{len(cells)}: {result.note}")
            kept = finalize_rows(rows, query, bounding_box)
            self._salvage = kept
            print(
                f"cell {index}/{len(cells)}: {len(result.rows)} rows, {len(kept)} kept so far"
            )
            if query.limit and len(kept) >= query.limit:
                logger.info("reached the row limit of %s; stopping", query.limit)
                print(f"✓ reached --limit {query.limit}; stopping")
                break

            if (
                query.deep_scan
                and not cell.get("_sub")
                and len(result.rows) >= SATURATED
            ):
                sub_cells: list[GridCell] = [
                    {**cell, "_sub": True}
                    for cell in build_grid_cells(cell["_bbox"], 2)
                ]
                logger.info(
                    "cell %d is at Google's cap (%d rows): %d sub-tiles added",
                    index,
                    len(result.rows),
                    len(sub_cells),
                )
                cells.extend(sub_cells)  # one level only: sub-tiles never split again
                print(
                    f"! cell {index} is at Google's cap: {len(sub_cells)} sub-tiles added, {len(cells)} cells now"
                )

        kept = finalize_rows(rows, query, bounding_box)
        logger.info(
            "grid done: %d finished, %d skipped, blocked=%s, %d unique rows, %d kept",
            finished,
            skipped,
            blocked,
            len(rows),
            len(kept),
        )
        print(f"{len(rows)} unique rows, {len(kept)} after the city box and filters")

        return ScrapeResult(kept, columns, finished, skipped, blocked)
