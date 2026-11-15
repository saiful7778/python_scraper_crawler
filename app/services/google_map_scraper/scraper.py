"""Scrapes the keywords in one city, as a single scrape or a grid of scrapes, and stores the leads."""

import queue
import threading
import time
from functools import partial
from typing import cast

from redis import Redis
from redis.exceptions import RedisError

from app.repositories import GoogleMapProfileRepository, LeadRepository
from app.services.browser import BrowserSession, CDPError, SharedBrowser
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
from .place_queue import DoneKeyword, PlaceJob, PlaceJobQueue
from .scrape.dedupe import dedupe_rows, lead_keys
from .scrape.engine_config import EngineConfig
from .scrape.lead_filter import keep_lead
from .scrape.lead_rows import result_columns
from .scrape.scrape_database import ScrapeDatabase, ScrapeDatabaseError
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
        self._shared = SharedBrowser(locale, config.headless)
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
                    browser=BrowserSession(
                        locale, config.headless, shared=self._shared
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
            ("shared browser", self._shared.close),
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

    def _check_workers(self, scrape_id: str) -> None:
        for worker in self._workers:
            if not worker.is_alive():
                if worker.failure is not None:
                    raise worker.failure
                raise ScrapeWorkerDiedError(
                    f"the scrape worker stopped while scrape {scrape_id} had no outcome"
                )

    def _wait_for_scrape(self, scrape_id: str) -> None:
        while scrape_id not in self._finished:
            self._check_exists(scrape_id)
            self._check_workers(scrape_id)
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


class PlaceScraper:
    """Scrapes the keywords in one city, one queued job per keyword, each as a single scrape or a grid."""

    def __init__(self, redis: Redis | None = None) -> None:
        self._redis = redis or connect(RedisConfig())
        self._salvage: list[LeadRow] = []
        self._stop = False
        self._cancel = threading.Event()

    @property
    def salvage_rows(self) -> list[LeadRow]:
        """The leads kept so far, so a Ctrl-C can still save them."""
        return self._salvage

    def scrape(
        self, query: PlaceQuery, known_keys: frozenset[str] = frozenset()
    ) -> ScrapeResult:
        """Queue one job per keyword, or resume the queue an earlier run of this query left, and run it.

        The queue is deleted when the run ends or a rate limit or lost Chrome
        connection stops it. A crash or an exception leaves it for the next run.
        """
        columns = result_columns(query.engine_settings())
        self._salvage = []
        queue = PlaceJobQueue(self._redis, query, BUFFER_TTL_SECONDS)

        if queue.exists():
            logger.info("resuming the unfinished place run for %r", query.keywords)
            print("▶ Resuming the unfinished run of this command")
        else:
            early = self._enqueue_run(query, columns, queue)
            if early is not None:
                return early

        return self._listen(query, known_keys, columns, queue)

    def _enqueue_run(
        self, query: PlaceQuery, columns: list[str], queue: PlaceJobQueue
    ) -> ScrapeResult | None:
        """Geocode the city and queue one job per keyword; a result means the run ends here."""
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
                kw_settings = anchored.model_copy(update={"keywords": (keyword,)})
                self._scrape_keyword(query, kw_settings, name, bounding_box, queue)
        except BaseException:
            # a partly queued run must not be resumed as if it were whole
            try:
                queue.clear()
            except Exception:
                logger.exception("clearing the partly queued place run failed")
            raise
        return None

    def _scrape_keyword(
        self,
        query: PlaceQuery,
        settings: ScrapeSettings,
        name: str,
        bounding_box: BoundingBox | None,
        queue: PlaceJobQueue,
    ) -> None:
        """Queue one keyword as a job, with a grid of cells when the query has a grid.

        :raises BufferError: If Redis fails.
        """
        cells = (
            build_grid_cells(bounding_box, query.grid)
            if query.grid and bounding_box
            else None
        )
        job: PlaceJob = {
            "keyword": settings.keywords[0],
            "name": name,
            "settings": cast(
                SettingsData, settings.model_dump(by_alias=True, mode="json")
            ),
            "bounding_box": bounding_box,
            "cells": cells,
            "next_cell": 0,
            "rows": [],
            "kept": [],
            "finished": 0,
            "skipped": 0,
        }
        queue.enqueue(job)

    def _listen(
        self,
        query: PlaceQuery,
        known_keys: frozenset[str],
        columns: list[str],
        queue: PlaceJobQueue,
    ) -> ScrapeResult:
        """Run the queued jobs one at a time, first in first out, until the queue is empty or a job stops the run."""
        self._stop = False
        self._cancel.clear()
        total = len(query.keywords)
        while (job := queue.peek()) is not None:
            earlier = queue.done()
            print(f"▶ Keyword {len(earlier) + 1}/{total}: {job['keyword']}")
            saved = cast(
                list[LeadRow], [row for item in earlier for row in item["kept"]]
            )
            known = known_keys | frozenset(
                key for row in saved for key in lead_keys(row)
            )
            self._salvage = saved
            done = self._run_job(query, job, queue, known, saved)
            queue.complete(done)
            if done["blocked"] or self._stop:
                break

        results = queue.done()
        all_kept = cast(
            list[LeadRow], [row for item in results for row in item["kept"]]
        )
        finished = sum(item["finished"] for item in results)
        skipped = sum(item["skipped"] for item in results)
        blocked = any(item["blocked"] for item in results)
        queue.clear()
        logger.info(
            "keywords done: %d finished, %d skipped, blocked=%s, %d kept",
            finished,
            skipped,
            blocked,
            len(all_kept),
        )
        return ScrapeResult(all_kept, columns, finished, skipped, blocked)

    def _run_job(
        self,
        query: PlaceQuery,
        job: PlaceJob,
        queue: PlaceJobQueue,
        known_keys: frozenset[str],
        saved: list[LeadRow],
    ) -> DoneKeyword:
        """Run a job on its own thread, wait for it, and raise what it raised."""
        outcome: dict[str, DoneKeyword] = {}
        failure: dict[str, BaseException] = {}

        def target() -> None:
            try:
                if job["cells"] is None:
                    outcome["done"] = self._scrape_single(query, job, known_keys, saved)
                else:
                    outcome["done"] = self._scrape_grid(
                        query, job, queue, known_keys, saved
                    )
            except BaseException as error:
                failure["error"] = error

        thread = threading.Thread(
            target=target, name=f"place-job-{job['keyword']}", daemon=True
        )
        thread.start()
        try:
            thread.join()
        except KeyboardInterrupt:
            # let the cell in flight finish; the job stops before the next one
            self._cancel.set()
            print("⏸ Finishing the current cell, press Ctrl-C again to quit")
            thread.join()
            raise
        if "error" in failure:
            raise failure["error"]
        return outcome["done"]

    def _scrape_single(
        self,
        query: PlaceQuery,
        job: PlaceJob,
        known_keys: frozenset[str],
        saved: list[LeadRow],
    ) -> DoneKeyword:
        """Scrape a job's keyword as one scrape, with no progress saved.

        :raises UnsavedLeadsError: If the scrape's leads stay unsaved in Redis.
        """
        if self._cancel.is_set():
            raise KeyboardInterrupt()
        settings = ScrapeSettings.model_validate(job["settings"])
        name = job["name"]
        print(
            f"▶ Creating scrape: 1 keyword @ {settings.latitude},{settings.longitude} depth={query.scroll_depth}"
        )
        logger.info("single scrape at %s,%s", settings.latitude, settings.longitude)
        try:
            result = run_scrape(name, settings, known_keys=known_keys)
        except ScrapeDeletedError as exc:
            print(f"\n⚠ keyword skipped: {describe(exc)}")
            logger.warning("single scrape deleted: %s", describe(exc))
            return {"kept": [], "finished": 0, "skipped": 1, "blocked": False}
        except CDPError as exc:
            print(f"\n✗ Stopped: Chrome connection lost ({describe(exc)})")
            logger.exception("single scrape stopped on a lost Chrome connection")
            self._stop = True
            return {"kept": [], "finished": 0, "skipped": 1, "blocked": False}
        except (BufferError, ScrapeDatabaseError, UnsavedLeadsError):
            raise
        except Exception as exc:
            print(f"\n⚠ keyword skipped: {describe(exc)}")
            logger.exception("single scrape skipped")
            return {"kept": [], "finished": 0, "skipped": 1, "blocked": False}

        kept = finalize_rows(dedupe_rows(result.rows, known_keys), query, None)
        self._salvage = saved + kept
        logger.info(
            "single scrape: %d rows, %d kept after dedupe and filters",
            len(result.rows),
            len(kept),
        )
        if result.note:
            logger.info("single scrape note: %s", result.note)
            print(f"⚠ {result.note}")
        if result.status == ScrapeStatus.FAILED:
            logger.warning("single scrape failed: %s", result.note)
            print(f"✗ {result.note}")
            return {
                "kept": kept,
                "finished": 0,
                "skipped": 1,
                "blocked": result.blocked,
            }
        return {
            "kept": kept,
            "finished": 1,
            "skipped": 0,
            "blocked": result.blocked,
        }

    def _scrape_grid(
        self,
        query: PlaceQuery,
        job: PlaceJob,
        queue: PlaceJobQueue,
        known_keys: frozenset[str],
        saved: list[LeadRow],
    ) -> DoneKeyword:
        """Scrape a job's keyword as a grid of scrapes, saving the job before each cell.

        Each grid tile is limited by its own rows, and tiles dedupe against
        each other and against ``known_keys``. A job read back from the queue
        starts at ``next_cell`` with the rows it had then.

        :raises UnsavedLeadsError: If a scrape's leads stay unsaved in Redis.
        """
        settings = ScrapeSettings.model_validate(job["settings"])
        name = job["name"]
        assert (
            job["bounding_box"] is not None
        )  # Grid jobs carry the resolved bounding box.
        assert job["cells"] is not None
        south, north, west, east = job["bounding_box"]
        bounding_box: BoundingBox = (south, north, west, east)
        cells = cast(
            list[GridCell],
            [{**cell, "_bbox": tuple(cell["_bbox"])} for cell in job["cells"]],
        )
        deep_scan_note = (
            " (! deep scan: a tile at Google's cap is split into 4 sub-tiles)"
            if query.deep_scan
            else ""
        )
        print(
            f"▶ Grid {query.grid}x{query.grid}: {len(cells)} scrapes, one at a time, depth={query.scroll_depth}{deep_scan_note}"
        )

        rows = job["rows"]
        kept = job["kept"]
        finished = job["finished"]
        skipped = job["skipped"]
        blocked = False
        lost_chrome = False
        index = job["next_cell"]
        self._salvage = saved + kept

        # Sequential on purpose: never run parallel scrapes against Google.
        while index < len(cells):
            if self._cancel.is_set():
                raise KeyboardInterrupt()
            job.update(
                next_cell=index,
                rows=job["rows"],
                kept=kept,
                finished=finished,
                skipped=skipped,
                cells=cells,
            )
            queue.save(job)
            cell = cells[index]
            index += 1
            sub_tag = " (sub-tile)" if cell.get("_sub") else ""
            print(
                f"▶ Cell {index}/{len(cells)} @ {cell['lat']},{cell['lon']} radius={cell['radius']}m{sub_tag}"
            )
            cell_settings = settings.model_copy(
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
                lost_chrome = True
                break
            except (BufferError, ScrapeDatabaseError, UnsavedLeadsError):
                raise
            except Exception as exc:
                print(f"\n⚠ cell {index}/{len(cells)} skipped: {describe(exc)}")
                logger.exception("cell %d skipped", index)
                skipped += 1
                continue

            # known_keys may already hold the leads this job saved before it was resumed
            seen = known_keys | frozenset(key for row in rows for key in lead_keys(row))
            fresh = dedupe_rows(result.rows, seen)
            rows.extend(fresh)
            kept.extend(finalize_rows(fresh, query, bounding_box))
            self._salvage = saved + kept
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
            print(
                f"cell {index}/{len(cells)}: {len(result.rows)} rows, {len(kept)} kept so far"
            )

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

        logger.info(
            "grid done: %d finished, %d skipped, blocked=%s, %d unique rows, %d kept",
            finished,
            skipped,
            blocked,
            len(rows),
            len(kept),
        )
        print(f"{len(rows)} unique rows, {len(kept)} after the city box and filters")

        self._stop = self._stop or blocked or lost_chrome
        return {
            "kept": kept,
            "finished": finished,
            "skipped": skipped,
            "blocked": blocked,
        }
