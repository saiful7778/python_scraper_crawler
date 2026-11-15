"""Scrapes the keywords in one city as tiles across proxy slots, and stores the leads."""

import queue
import threading
import time
from dataclasses import dataclass
from functools import partial
from typing import cast
from uuid import uuid4

from redis import Redis
from redis.exceptions import RedisError

from app.repositories import GoogleMapProfileRepository, LeadRepository
from app.services.browser import BrowserSession, SharedBrowser
from app.services.buffer.client import BufferError, connect
from app.services.buffer.discard import discard_scrape
from app.services.buffer.flusher import Flusher
from app.services.buffer.row_buffer import RowBuffer
from app.services.buffer.scrape_state import ScrapeState
from app.services.buffer.website_queue import WebsiteQueue
from app.utils.errors import ScraperError, describe
from app.utils.failure_messages import is_proxy_failure
from app.utils.logger import get_logger
from app.utils.proxies import mask_proxy_credentials
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
from .place_queue import PlaceTile, PlaceTileQueue
from .scrape.dedupe import dedupe_rows, lead_keys
from .scrape.engine_config import EngineConfig
from .scrape.lead_filter import keep_lead
from .scrape.lead_rows import result_columns
from .scrape.scrape_database import ScrapeDatabase
from .scrape.scrape_limits import BUFFER_TTL_SECONDS, MAX_RESUMES
from .scrape.scrape_run import RunOutcome
from .scrape.scrape_settings import ScrapeSettings
from .scrape.scrape_status import ScrapeStatus
from .scrape.scrape_types import LeadRow, RunResult, ScrapeResult, SettingsData
from .scrape.scrape_worker import ScrapeWorker
from .scrape.website_resolver import WebsiteResolutions

logger = get_logger(__name__)

SATURATED = 20  # rows per scrape that indicates a tile hit Google's cap

_POLL_SECONDS = 1
_EXISTENCE_POLLS = 5  # idle polls between checks that the queued scrapes still exist


class BoundingBoxError(ScraperError):
    """Nominatim's bounding box is not four numbers."""


class ScrapeDeletedError(ScraperError):
    """The scrape was deleted while it was running, so it has no result."""


class ScrapeWorkerDiedError(ScraperError):
    """The scrape worker thread stopped before the scrape reached an outcome."""


class UnsavedLeadsError(ScraperError):
    """The final save left the scrape's leads in Redis, so they are not in PostgreSQL."""

    def __init__(self, scrape_id: str, count: int):
        super().__init__(
            f"{count} lead(s) of scrape {scrape_id} are not saved to PostgreSQL and stay "
            "in Redis; they are saved when the scrape resumes"
        )


def columns_for(query: PlaceQuery) -> list[str]:
    """The CSV columns a query produces."""
    return result_columns(query.engine_settings())


def finalize_rows(
    rows: list[LeadRow], query: PlaceQuery, bounding_box: BoundingBox | None
) -> list[LeadRow]:
    kept = [
        row
        for row in rows
        if (bounding_box is None or is_in_bounding_box(row, bounding_box))
        and keep_lead(row, query)
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


FinishedTile = tuple[str, RunOutcome, list[LeadRow], int]


class ScrapeSession:
    """The threads and Redis buffers of one ``run_scrape`` call, or of one city run.

    One worker runs per proxy slot. All of them attach to one shared browser and
    each takes the scrape ids submitted to its slot.
    """

    def __init__(
        self,
        config: EngineConfig,
        redis: Redis,
        postgres: TortoiseConfig,
        known_keys: frozenset[str],
        slots: int,
        *,
        locale: str,
    ):
        self._redis = redis
        self._state = ScrapeState(redis, BUFFER_TTL_SECONDS)
        self._rows = RowBuffer(redis, BUFFER_TTL_SECONDS)
        self._queue = WebsiteQueue(redis, BUFFER_TTL_SECONDS)
        self._finished: dict[str, tuple[RunOutcome, list[LeadRow]]] = {}
        self._finished_queue: queue.Queue[FinishedTile] = queue.Queue()
        self._deleted: set[str] = set()
        self._database = ScrapeDatabase(postgres)
        self._shared = SharedBrowser(locale, config.headless) if slots > 1 else None
        self._assignments: list[queue.Queue[str]] = []
        self._workers: list[ScrapeWorker] = []
        for slot in range(slots):
            assignments: queue.Queue[str] = queue.Queue()
            self._assignments.append(assignments)
            self._workers.append(
                ScrapeWorker(
                    self._database,
                    redis,
                    headless=config.headless,
                    known_keys=known_keys,
                    on_finished=partial(self._keep_finished, slot=slot),
                    assignments=assignments,
                    browser=(
                        BrowserSession(locale, config.headless, shared=self._shared)
                        if self._shared is not None
                        else None
                    ),
                    proxy_slot=slot,
                )
            )
        profiles = GoogleMapProfileRepository(LeadRepository())
        self._flusher = Flusher(
            self._rows,
            self._state,
            save_row=profiles.save_row,
            crawl_sink=None,
            config=postgres,
            prepare_batch=WebsiteResolutions(),
            save_rows=profiles.save_rows,
        )

    def start(self) -> list[str]:
        """Resume interrupted scrapes, start the Flusher and the workers; returns the carried ids."""
        carried = self._resume_interrupted()
        self._flusher.start()
        for worker in self._workers:
            worker.start()
        return carried

    def submit(self, scrape_id: str, slot: int) -> None:
        """Give a scrape id to the worker of ``slot``."""
        self._assignments[slot].put(scrape_id)

    def next_finished(self, timeout: float) -> FinishedTile | None:
        """The next tile that finished, with its slot; None when none finishes within ``timeout``."""
        try:
            return self._finished_queue.get(timeout=timeout)
        except queue.Empty:
            return None

    def finish(self, scrape_id: str) -> RunResult | None:
        """Save and finish one tile's leads and return its result; None if it has no outcome yet.

        :raises UnsavedLeadsError: If the tile's leads stay unsaved in Redis.
        :raises ScrapeDeletedError: If the tile was deleted while it ran.
        """
        if scrape_id not in self._finished:
            return None
        return self._finish_one(scrape_id)

    def create(self, name: str, settings: SettingsData) -> str:
        """Store a new pending scrape and return its id; a worker runs it once submitted."""
        return self._database.create(name, settings)

    def proxies_of(self, scrape_id: str) -> tuple[str, ...] | None:
        """The proxies a stored scrape was created with; None if the scrape does not exist."""
        record = self._database.get(scrape_id)
        if record is None:
            return None
        return tuple(record["Data"].get("proxies", ()))

    def check_workers(self) -> None:
        """Raise if a worker stopped while scrapes are still waiting for an outcome."""
        self._check_workers("a queued scrape")

    def run(self, name: str, settings: ScrapeSettings) -> RunResult:
        carried = self.start()
        scrape_id = self._database.create(
            name, cast(SettingsData, settings.model_dump(by_alias=True, mode="json"))
        )
        for earlier in carried:
            self.submit(earlier, 0)
        self.submit(scrape_id, 0)
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
            ("scrape workers", self._shutdown_workers),
            ("shared browser", lambda: self._shared and self._shared.close()),
            ("flusher", lambda: self._flusher.shutdown(wait=None)),
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

    def _shutdown_workers(self) -> None:
        for worker in self._workers:
            worker.shutdown()
        for worker in self._workers:
            if worker.is_alive():
                worker.join(1)

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
        self, scrape_id: str, outcome: RunOutcome, rows: list[LeadRow], slot: int
    ) -> None:
        self._finished[scrape_id] = (outcome, rows)
        self._finished_queue.put((scrape_id, outcome, rows, slot))

    def _check_exists(self, scrape_id: str) -> None:
        if self._database.get(scrape_id) is None:
            self._deleted.add(scrape_id)
            for worker in self._workers:
                worker.cancel(scrape_id)
            raise ScrapeDeletedError(f"scrape {scrape_id} was deleted while it ran")

    def _check_workers(self, waiting: str) -> None:
        for worker in self._workers:
            if not worker.is_alive():
                if worker.failure is not None:
                    raise worker.failure
                raise ScrapeWorkerDiedError(
                    f"the scrape worker stopped while {waiting} had no outcome"
                )

    def _wait_for_scrape(self, scrape_id: str) -> None:
        while scrape_id not in self._finished:
            self._check_exists(scrape_id)
            self._check_workers(f"scrape {scrape_id}")
            time.sleep(_POLL_SECONDS)

    def _drop(self, scrape_id: str) -> None:
        discard_scrape(self._redis, scrape_id)
        self._finished.pop(scrape_id, None)

    def _finish(self, carried: list[str], scrape_id: str) -> RunResult:
        ids = [i for i in (*carried, scrape_id) if i in self._finished]
        self._shutdown_workers()
        self._flusher.shutdown(wait=None)
        result: RunResult | None = None
        for finished_id in ids:
            if finished_id == scrape_id:
                result = self._finish_one(scrape_id)
                continue
            try:
                self._finish_one(finished_id)
            except (UnsavedLeadsError, ScrapeDeletedError):
                continue
        if result is None:
            raise ScrapeWorkerDiedError(f"scrape {scrape_id} ended without an outcome")
        return result

    def _finish_one(self, finished_id: str) -> RunResult:
        outcome, rows = self._finished[finished_id]
        progress = self._state.get_progress(finished_id)
        left = self._rows.length(finished_id)
        if left:
            logger.warning(
                "PostgreSQL did not take %d buffered lead(s) of scrape %s; they stay in Redis and the scrape resumes on the next start",
                left,
                finished_id,
            )
            raise UnsavedLeadsError(finished_id, left)
        discard_scrape(self._redis, finished_id)
        if not self._database.finish(
            finished_id, outcome.status, outcome.note, progress
        ):
            raise ScrapeDeletedError(f"scrape {finished_id} was deleted while it ran")
        return RunResult(
            scrape_id=finished_id,
            status=outcome.status,
            note=outcome.note,
            rows=rows,
            blocked=bool(progress.get("blocked")),
        )


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
    :raises UnsavedLeadsError: If the final save leaves the scrape's leads in Redis.
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
        1,
        locale=settings.locale_tag,
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


def _new_tile(
    name: str,
    keyword: str,
    settings: SettingsData,
    bounding_box: BoundingBox | None,
    cell: GridCell | None,
) -> PlaceTile:
    return {
        "ID": uuid4().hex,
        "keyword": keyword,
        "name": name,
        "settings": settings,
        "bounding_box": bounding_box,
        "cell": cell,
        "attempts": 0,
        "scrape_id": None,
    }


def _tile_settings(settings: SettingsData, cell: GridCell) -> SettingsData:
    """The settings of a tile at a grid cell: its centre and zoom replace the city's."""
    scraped = ScrapeSettings.model_validate(settings).model_copy(
        update={
            "latitude": float(cell["lat"]),
            "longitude": float(cell["lon"]),
            "zoom": cell["zoom"],
        }
    )
    return cast(SettingsData, scraped.model_dump(by_alias=True, mode="json"))


@dataclass
class _Flight:
    """A scrape submitted to a slot: its tile, or None for a carried scrape no tile owns."""

    tile: PlaceTile | None
    slot: int


class _TileRun:
    """Feeds the tile queue to the session's slots and folds each finished tile into the run.

    A slot runs one scrape at a time, so one proxy runs one tile at a time. A tile
    that its proxy blocks or fails on goes back to the front of the queue and runs
    on another slot; the slot that failed takes no more tiles.
    """

    def __init__(
        self,
        query: PlaceQuery,
        session: ScrapeSession,
        tiles: PlaceTileQueue,
        known_keys: frozenset[str],
        lanes: list[tuple[str, ...]],
        cancel: threading.Event,
    ):
        self._query = query
        self._session = session
        self._tiles = tiles
        self._lanes = lanes
        self._cancel = cancel
        self._flights: dict[str, _Flight] = {}
        self._load = [0] * len(lanes)
        self._retired: set[int] = set()
        done = tiles.done()
        self.kept: list[LeadRow] = [row for item in done for row in item["kept"]]
        self._seen = set(known_keys) | {
            key for row in self.kept for key in lead_keys(row)
        }
        self.finished = sum(item["finished"] for item in done)
        self.skipped = sum(item["skipped"] for item in done)
        self.blocked = any(item["blocked"] for item in done)
        self.stopped = False

    def start(self, carried: list[str]) -> None:
        """Resubmit the in-flight tiles by their scrape ids, and discard carried scrapes no tile owns."""
        orphans = list(carried)
        for tile in self._tiles.in_flight():
            scrape_id = tile["scrape_id"]
            if scrape_id is None or scrape_id not in orphans:
                logger.info(
                    "tile %s was in flight without a live scrape; it runs again",
                    tile["ID"],
                )
                self._tiles.requeue(tile["ID"], {**tile, "scrape_id": None})
                continue
            orphans.remove(scrape_id)
            self._submit(scrape_id, self._slot_for(scrape_id), tile)
        for scrape_id in orphans:
            logger.warning(
                "scrape %s matches no queued tile; it is finished and its leads are discarded",
                scrape_id,
            )
            self._submit(scrape_id, self._slot_for(scrape_id), None)

    def run(self) -> None:
        """Run tiles until the queue is empty, every slot is retired, or the run stops."""
        try:
            self._fill()
            self._drain()
        except KeyboardInterrupt:
            self._cancel.set()
            print("⏸ Finishing the tiles in flight, press Ctrl-C again to quit")
            self._drain()
            raise

    def _drain(self) -> None:
        idle = 0
        while self._flights:
            item = self._session.next_finished(_POLL_SECONDS)
            if item is None:
                self._session.check_workers()
                idle += 1
                if idle % _EXISTENCE_POLLS == 0 and self._drop_deleted():
                    self._fill()
                continue
            self._handle(item)
            self._fill()

    def _drop_deleted(self) -> bool:
        """Stop waiting for flights whose scrape was deleted, since no worker reports them. True if any."""
        dropped = False
        for scrape_id, flight in list(self._flights.items()):
            if self._session.proxies_of(scrape_id) is not None:
                continue
            del self._flights[scrape_id]
            self._load[flight.slot] -= 1
            dropped = True
            logger.info("scrape %s was deleted while it was queued", scrape_id)
            if flight.tile is not None:
                print("\n⚠ keyword skipped: the scrape was deleted while it was queued")
                self._finish_tile(
                    flight.tile, [], [], finished=0, skipped=1, blocked=False
                )
                self.skipped += 1
        return dropped

    def _slot_for(self, scrape_id: str) -> int:
        proxies = self._session.proxies_of(scrape_id) or ()
        return self._lanes.index(proxies) if proxies in self._lanes else 0

    def _fill(self) -> None:
        for slot in range(len(self._lanes)):
            if self._cancel.is_set() or self.stopped:
                return
            if slot in self._retired or self._load[slot]:
                continue
            tile = self._tiles.take()
            if tile is None:
                return
            self._start_tile(tile, slot)

    def _start_tile(self, tile: PlaceTile, slot: int) -> None:
        lane = self._lanes[slot]
        settings = cast(SettingsData, {**tile["settings"], "proxies": lane})
        scrape_id = self._session.create(tile["name"], settings)
        self._tiles.mark_scrape(tile["ID"], scrape_id)
        cell = tile["cell"]
        if cell is None:
            print(
                f"▶ Scraping {tile['keyword']}"
                + (f" through {mask_proxy_credentials(lane[0])}" if lane else "")
            )
        else:
            sub_tag = " (sub-tile)" if cell.get("_sub") else ""
            print(
                f"▶ Cell @ {cell['lat']},{cell['lon']} radius={cell['radius']}m{sub_tag}"
                + (f" through {mask_proxy_credentials(lane[0])}" if lane else "")
            )
        self._submit(scrape_id, slot, {**tile, "scrape_id": scrape_id})

    def _submit(self, scrape_id: str, slot: int, tile: PlaceTile | None) -> None:
        self._session.submit(scrape_id, slot)
        self._flights[scrape_id] = _Flight(tile, slot)
        self._load[slot] += 1

    def _handle(self, item: FinishedTile) -> None:
        scrape_id, outcome, rows, slot = item
        flight = self._flights.pop(scrape_id, None)
        if flight is None:
            logger.info(
                "scrape %s reported after it was dropped; its leads are not kept",
                scrape_id,
            )
            return
        self._load[slot] -= 1
        if flight.tile is None:
            self._discard(scrape_id)
            return
        tile = flight.tile
        try:
            result = self._session.finish(scrape_id)
        except ScrapeDeletedError as error:
            print(f"\n⚠ keyword skipped: {describe(error)}")
            logger.warning("tile scrape deleted: %s", describe(error))
            self._finish_tile(tile, [], [], finished=0, skipped=1, blocked=False)
            self.skipped += 1
            return
        if result is None:
            raise ScrapeWorkerDiedError(f"scrape {scrape_id} ended without an outcome")

        blocked = result.blocked
        proxy_failed = (
            bool(self._lanes[slot])
            and not blocked
            and outcome.status == ScrapeStatus.FAILED
            and is_proxy_failure(outcome.note)
        )
        fresh = dedupe_rows(rows, frozenset(self._seen))
        kept = finalize_rows(fresh, self._query, _box(tile["bounding_box"]))
        self.kept.extend(kept)
        self._seen.update(key for row in kept for key in lead_keys(row))

        if outcome.connection_lost:
            # The browser is shared, so every slot is affected: stop, keep the tile in flight
            self.stopped = True
            self.skipped += 1
            print(f"\n✗ Stopped: Chrome connection lost ({outcome.note})")
            logger.warning("tile stopped on a lost Chrome connection: %s", outcome.note)
            return

        if blocked or proxy_failed:
            self._retired.add(slot)
            attempts = tile["attempts"] + 1
            proxy = (
                mask_proxy_credentials(self._lanes[slot][0])
                if self._lanes[slot]
                else "this IP"
            )
            cause = "was rate-limited" if blocked else "failed"
            if attempts < len(self._query.proxies):
                print(
                    f"\n⚠ {proxy} {cause}: its tiles stop; this tile runs on another proxy"
                )
                logger.warning("tile retried on another slot after %s %s", proxy, cause)
                self._tiles.requeue(
                    tile["ID"], {**tile, "attempts": attempts, "scrape_id": None}
                )
                return
            print(
                f"\n⚠ Stopped: {proxy} {cause}; the tiles after it were not searched."
            )
            logger.warning("tile skipped after %d attempt(s): %s", attempts, proxy)
            self._finish_tile(tile, fresh, kept, finished=0, skipped=1, blocked=blocked)
            self.skipped += 1
            self.blocked = self.blocked or blocked
            return

        if outcome.status == ScrapeStatus.FAILED:
            self._finish_tile(tile, fresh, kept, finished=0, skipped=1, blocked=False)
            self.skipped += 1
            print(f"\n  ⚠ tile skipped: {outcome.note}")
            return

        self._finish_tile(tile, fresh, kept, finished=1, skipped=0, blocked=False)
        self.finished += 1
        if outcome.note:
            print(f"⚠ {outcome.note}")
        print(f"tile: {len(rows)} rows, {len(self.kept)} kept so far")
        cell = tile["cell"]
        if (
            self._query.deep_scan
            and cell is not None
            and not cell.get("_sub")
            and len(rows) >= SATURATED
        ):
            self._split(tile, cell, len(rows))

    def _split(self, tile: PlaceTile, cell: GridCell, rows: int) -> None:
        bbox = cast(BoundingBox, tuple(cell["_bbox"]))
        sub_cells: list[GridCell] = [
            {**sub, "_sub": True} for sub in build_grid_cells(bbox, 2)
        ]  # one level only: sub-tiles never split again
        for sub in sub_cells:
            self._tiles.push(
                _new_tile(
                    tile["name"],
                    tile["keyword"],
                    _tile_settings(tile["settings"], sub),
                    tile["bounding_box"],
                    sub,
                )
            )
        logger.info(
            "tile at Google's cap (%d rows): %d sub-tiles added", rows, len(sub_cells)
        )
        print(f"! tile is at Google's cap: {len(sub_cells)} sub-tiles added")

    def _finish_tile(
        self,
        tile: PlaceTile,
        fresh: list[LeadRow],
        kept: list[LeadRow],
        *,
        finished: int,
        skipped: int,
        blocked: bool,
    ) -> None:
        self._tiles.finish(
            tile["ID"],
            {
                "kept": kept,
                "finished": finished,
                "skipped": skipped,
                "blocked": blocked,
                "rows": fresh,
            },
        )

    def _discard(self, scrape_id: str) -> None:
        try:
            self._session.finish(scrape_id)
        except (ScrapeDeletedError, UnsavedLeadsError) as error:
            logger.warning(
                "discarded scrape %s was not saved: %s", scrape_id, describe(error)
            )
            return
        logger.info("discarded carried scrape %s", scrape_id)


def _box(bounding_box: list[float] | None) -> BoundingBox | None:
    return None if bounding_box is None else cast(BoundingBox, tuple(bounding_box))


class PlaceScraper:
    """Scrapes the keywords in one city as tiles: one per keyword, or one per grid cell, run across proxy slots."""

    def __init__(self, redis: Redis | None = None) -> None:
        self._redis = redis or connect(RedisConfig())
        self._salvage: list[LeadRow] = []
        self._cancel = threading.Event()

    @property
    def salvage_rows(self) -> list[LeadRow]:
        """The leads kept so far, so a Ctrl-C can still save them."""
        return self._salvage

    def scrape(
        self, query: PlaceQuery, known_keys: frozenset[str] = frozenset()
    ) -> ScrapeResult:
        """Queue the tiles of each keyword, or resume the queue an earlier run of this query left, and run them.

        The queue is deleted when the run ends or every proxy is stopped. A lost
        Chrome connection, a crash or an exception leaves it for the next run.
        """
        columns = result_columns(query.engine_settings())
        self._salvage = []
        tiles = PlaceTileQueue(self._redis, query, BUFFER_TTL_SECONDS)

        if tiles.exists():
            logger.info("resuming the unfinished place run for %r", query.keywords)
            print("▶ Resuming the unfinished run of this command")
        else:
            early = self._enqueue_run(query, columns, tiles)
            if early is not None:
                return early

        return self._listen(query, known_keys, columns, tiles)

    def _enqueue_run(
        self, query: PlaceQuery, columns: list[str], tiles: PlaceTileQueue
    ) -> ScrapeResult | None:
        """Geocode the city and queue the tiles of every keyword; a result means the run ends here."""
        settings = query.engine_settings()
        place = ", ".join(part for part in (query.city, query.state) if part)

        logger.info(
            "scraping %r in %r: grid=%s depth=%s",
            query.keywords,
            place,
            query.grid,
            query.scroll_depth,
        )
        print(f'▶ Geocoding "{place}"')
        bounding_box: BoundingBox | None = None

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
                print(
                    f"\t→ bbox S{south} N{north} W{west} E{east}, {query.grid}x{query.grid} tiles"
                )
                logger.info("geocoded %r: %dx%d grid", place, query.grid, query.grid)
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
        try:
            for keyword in query.keywords:
                # the scrape name is what the web UI lists
                name = f"{keyword} @ {place}"[:80]
                self._queue_keyword(query, anchored, keyword, name, bounding_box, tiles)
        except BaseException:
            # a partly queued run must not be resumed as if it were whole
            try:
                tiles.clear()
            except Exception:
                logger.exception("clearing the partly queued place run failed")
            raise
        return None

    def _queue_keyword(
        self,
        query: PlaceQuery,
        anchored: ScrapeSettings,
        keyword: str,
        name: str,
        bounding_box: BoundingBox | None,
        tiles: PlaceTileQueue,
    ) -> None:
        """Queue a keyword's tiles: one whole-city tile, or one per grid cell. Tiles start with no proxy.

        :raises BufferError: If Redis fails.
        """
        base = cast(
            SettingsData,
            anchored.model_copy(
                update={"keywords": (keyword,), "proxies": ()}
            ).model_dump(by_alias=True, mode="json"),
        )
        cells = (
            build_grid_cells(bounding_box, query.grid)
            if query.grid and bounding_box
            else None
        )
        if cells is None:
            tiles.push(_new_tile(name, keyword, base, bounding_box, None))
            return
        for cell in cells:
            tiles.push(
                _new_tile(name, keyword, _tile_settings(base, cell), bounding_box, cell)
            )

    def _listen(
        self,
        query: PlaceQuery,
        known_keys: frozenset[str],
        columns: list[str],
        tiles: PlaceTileQueue,
    ) -> ScrapeResult:
        """Run the queued tiles on one slot per proxy, then clear the queue unless a lost Chrome connection stopped the run."""
        self._cancel.clear()
        lanes = [(proxy,) for proxy in query.proxies] or [()]
        session = ScrapeSession(
            EngineConfig(),
            self._redis,
            TortoiseConfig(),
            known_keys,
            len(lanes),
            locale=query.locale_tag,
        )
        try:
            carried = session.start()
            run = _TileRun(query, session, tiles, known_keys, lanes, self._cancel)
            self._salvage = run.kept
            run.start(carried)
            run.run()
        finally:
            session.stop()

        if not run.stopped:
            tiles.clear()
        logger.info(
            "tiles done: %d finished, %d skipped, blocked=%s, %d kept",
            run.finished,
            run.skipped,
            run.blocked,
            len(run.kept),
        )
        return ScrapeResult(run.kept, columns, run.finished, run.skipped, run.blocked)
