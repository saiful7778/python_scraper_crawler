"""SQLite-backed storage of scrapes and their result rows.

Tortoise ORM is asynchronous, so :class:`ScrapeStore` runs a private asyncio loop
on a daemon thread and exposes a plain synchronous API to the rest of the
engine.  Every mutation publishes a fresh snapshot to the subscribers (the
live scrape list shown in the UI).
"""

import asyncio
import os
import queue
import sqlite3
import threading
import uuid
from collections.abc import Coroutine
from typing import Any, TypeVar, cast

from tortoise import Tortoise
from tortoise.context import TortoiseContext
from tortoise.transactions import in_transaction

from app.services.crawler.storage.crawl_store import (
    remove_scrape_websites,
    remove_websites_from,
    select_website_values,
)
from app.services.crawler.storage.models import WEBSITE_FIELDS
from app.utils.errors import ScraperError, describe
from app.utils.logger import get_logger

from .lead_rows import address_fields
from .scrape_models import (
    SCHEMA_VERSION,
    ScrapeAddressModel,
    ScrapeKeywordModel,
    ScrapeLeadModel,
    ScrapeModel,
    ScrapeProxyModel,
    ScrapeSettingsModel,
    lead_of,
    settings_columns,
    settings_of,
)
from .scrape_presenter import to_public_scrape
from .scrape_status import ScrapeStage, ScrapeStatus
from .scrape_types import (
    LeadRow,
    ProgressData,
    PublicScrape,
    ScrapeRecord,
    SettingsData,
)

logger = get_logger(__name__)

_ERROR_MAX_LENGTH = 500
#: Seconds the starting thread waits for the database to open.
_STARTUP_TIMEOUT_SECONDS = 60
#: Seconds between checks that the database thread is still alive during a call.
_CALL_POLL_SECONDS = 1.0
#: Seconds to wait for the database thread to stop on close.
_CLOSE_TIMEOUT_SECONDS = 5
#: Related rows a scrape record is built from.
_SCRAP_CHILDREN = ("settings_row", "keywords", "proxies")


class ScrapeStoreError(ScraperError):
    """The scrape database could not be opened, or the thread serving it stopped."""


MAX_RESUMES = 3  # restarts of the engine mid-scrape before the scrape is failed instead of resumed

#: Progress keys used by the engine, and the columns that hold them.
PROGRESS_COLUMNS: dict[str, str] = {
    "stage": "stage",
    "done": "keywords_done",
    "saved": "rows_saved",
    "leads": "leads",
    "percent": "percent",
    "failed": "failed_count",
    "last_error": "last_error",
    "blocked": "blocked",
    "retried": "retries",
    "timed_out": "timed_out",
    "elapsed": "elapsed_seconds",
    "reviews_done": "reviews_done",
    "reviews_total": "reviews_total",
    "resumes": "resumes",
}

T = TypeVar("T")


class StoreCloseHandle:
    """Object exposed as ``ScrapeStore.db`` so callers can close the store uniformly."""

    def __init__(self, store: "ScrapeStore"):
        """Remember the store to close."""
        self._store = store

    def close(self) -> None:
        """Close the underlying store."""
        self._store.close()


def _progress_values(progress: ProgressData) -> dict[str, object]:
    return {
        PROGRESS_COLUMNS[key]: value
        for key, value in progress.items()
        if key in PROGRESS_COLUMNS
    }


def _progress_of(record: ScrapeModel) -> ProgressData:
    progress = ProgressData(
        done=record.keywords_done,
        saved=record.rows_saved,
        leads=record.leads,
        percent=record.percent,
        failed=record.failed_count,
        last_error=record.last_error,
        blocked=record.blocked,
        retried=record.retries,
        timed_out=record.timed_out,
        elapsed=record.elapsed_seconds,
        reviews_done=record.reviews_done,
        reviews_total=record.reviews_total,
        resumes=record.resumes,
    )
    if record.stage:
        progress["stage"] = record.stage
    return progress


def database_path(directory: str) -> str:
    """Return the path of the scrape database inside ``directory``."""
    return os.path.join(directory, "scrapes.db")


def _reset_if_outdated(path: str) -> None:
    """Delete a database file written with another schema version."""
    if not os.path.exists(path):
        return
    connection = sqlite3.connect(path)
    try:
        version = connection.execute("PRAGMA user_version").fetchone()[0]
    finally:
        connection.close()
    if version != SCHEMA_VERSION:
        logger.warning(
            "scrape database %s has schema version %s, expected %s; deleting it and starting empty",
            path,
            version,
            SCHEMA_VERSION,
        )
        os.remove(path)


class ScrapeStore:
    """Synchronous facade over the scrape tables, with change notifications."""

    def __init__(self, directory: str):
        """Open (creating if needed) ``scrapes.db`` inside ``directory``.

        Raises:
            Exception: whatever the database startup raised, re-raised on the
                calling thread.
        """
        os.makedirs(directory, exist_ok=True)
        self.directory = directory
        self.lock = threading.RLock()
        self.db = StoreCloseHandle(self)
        self._ready = threading.Event()
        self._closed = False
        self._startup_error: BaseException | None = None
        self._listeners: set[queue.Queue[list[PublicScrape]]] = set()
        self._listeners_lock = threading.Lock()
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._serve_loop, name="engine-db", daemon=True
        )
        self._thread.start()
        if not self._ready.wait(_STARTUP_TIMEOUT_SECONDS):
            raise ScrapeStoreError(
                f"the scrape database at {database_path(directory)} did not open within "
                f"{_STARTUP_TIMEOUT_SECONDS} s (is another process locking it?)"
            )
        if self._startup_error:
            raise ScrapeStoreError(
                f"could not open the scrape database at {database_path(directory)}: "
                f"{describe(self._startup_error)}"
            ) from self._startup_error

    # ── event loop plumbing ──
    def _serve_loop(self) -> None:
        """Thread body: start the database, then run the loop until closed."""
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._open_database())
        except BaseException as error:  # noqa: BLE001 - reported to the starting thread
            self._startup_error = error
        self._ready.set()
        try:
            if not self._startup_error:
                self._loop.run_forever()
            self._loop.run_until_complete(Tortoise.close_connections())
        except Exception:
            logger.exception("closing the scrape database connections failed")
        finally:
            self._loop.close()

    async def _open_database(self) -> None:
        """Initialise Tortoise and create missing tables."""
        path = database_path(self.directory)
        _reset_if_outdated(path)
        self._context = TortoiseContext()
        self._context.__enter__()
        await self._context.init(
            db_url="sqlite://" + path,
            modules={
                "engine_scrapes": [
                    "app.services.google_map_scraper.scrape.scrape_models"
                ],
                "engine_crawler": ["app.services.crawler.storage.models"],
            },
            timezone="UTC",
        )
        await self._context.generate_schemas(safe=True)
        await Tortoise.get_connection("default").execute_script(
            f"PRAGMA user_version = {SCHEMA_VERSION}"
        )

    async def _run_in_context(self, coroutine: Coroutine[object, object, T]) -> T:
        """Run a coroutine with this store's Tortoise context active."""
        from tortoise.context import _current_context

        token = _current_context.set(self._context)
        try:
            return await coroutine
        finally:
            _current_context.reset(token)

    def _call(self, coroutine: Coroutine[object, object, T]) -> T:
        """Run a coroutine on the store loop and wait for its result.

        :raises ScrapeStoreError: If the store is closed or its thread stopped
            while the call was waiting.
        """
        if self._closed:
            raise ScrapeStoreError("the scrape store is closed; the call was not run")
        if not self._thread.is_alive():
            raise ScrapeStoreError(
                "the scrape database thread has stopped; the call was not run"
            )
        future = asyncio.run_coroutine_threadsafe(
            self._run_in_context(coroutine), self._loop
        )
        while True:
            try:
                return future.result(timeout=_CALL_POLL_SECONDS)
            except TimeoutError:
                if future.done():
                    # The coroutine itself raised TimeoutError.
                    raise
                if not self._thread.is_alive():
                    future.cancel()
                    raise ScrapeStoreError(
                        "the scrape database thread stopped while a call was waiting"
                    ) from None

    def run_async(self, coroutine: Coroutine[object, object, T]) -> T:
        """Run a coroutine on the store loop and wait for its result.

        :raises ScrapeStoreError: If the store is closed or its thread stopped.
        """
        return self._call(coroutine)

    def close(self) -> None:
        """Stop the loop thread; calling it twice is harmless."""
        if self._closed:
            return
        self._closed = True
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(_CLOSE_TIMEOUT_SECONDS)
        if self._thread.is_alive():
            logger.warning(
                "the scrape database thread did not stop within %s s; pending writes may be lost",
                _CLOSE_TIMEOUT_SECONDS,
            )

    # ── change notifications ──
    def subscribe(self) -> "queue.Queue[list[PublicScrape]]":
        """Register and return a queue that receives scrape-list snapshots."""
        listener: queue.Queue[list[PublicScrape]] = queue.Queue()
        with self._listeners_lock:
            self._listeners.add(listener)
        return listener

    def unsubscribe(self, listener: "queue.Queue[list[PublicScrape]]") -> None:
        """Stop sending snapshots to ``listener``."""
        with self._listeners_lock:
            self._listeners.discard(listener)

    def publish(self) -> None:
        """Send the current public scrape list to every subscriber."""
        with self._listeners_lock:
            listeners = tuple(self._listeners)
        if not listeners:
            return
        snapshot = [to_public_scrape(scrape) for scrape in self.list_scrapes()]
        for listener in listeners:
            listener.put(snapshot)

    # ── records ──
    @staticmethod
    def _record_to_scrape(record: ScrapeModel) -> ScrapeRecord:
        """Convert a ``ScrapeModel`` to the scrape dict used across the engine."""
        return {
            "ID": str(record.id),
            "Name": record.name,
            "Date": record.created_at.isoformat(timespec="seconds"),
            "Status": record.status,
            "Data": settings_of(record),
            "Error": record.error,
            "Progress": _progress_of(record),
        }

    async def _fetch_scrape(self, scrape_id: str) -> ScrapeModel | None:
        return await ScrapeModel.get_or_none(id=scrape_id).prefetch_related(
            *_SCRAP_CHILDREN
        )

    async def _insert_scrape(
        self, scrape_id: str, name: str, settings_data: SettingsData
    ) -> ScrapeModel:
        last = await ScrapeModel.all().order_by("-created_order").first()
        async with in_transaction():
            scrape = await ScrapeModel.create(
                id=scrape_id,
                name=name,
                status=ScrapeStatus.PENDING,
                created_order=(last.created_order + 1) if last else 1,
            )
            await ScrapeSettingsModel.create(
                scrape=scrape,
                **cast(dict[str, Any], settings_columns(settings_data)),
            )
            await ScrapeKeywordModel.bulk_create(
                ScrapeKeywordModel(scrape=scrape, position=position, term=term)
                for position, term in enumerate(settings_data["keywords"])
            )
            await ScrapeProxyModel.bulk_create(
                ScrapeProxyModel(scrape=scrape, position=position, url=url)
                for position, url in enumerate(settings_data.get("proxies", []))
            )
        return scrape

    def create_scrape(self, name: str, settings_data: SettingsData) -> str:
        """Queue a new scrape and return its id."""
        scrape_id = str(uuid.uuid4())
        self._call(self._insert_scrape(scrape_id, name, settings_data))
        self.publish()
        return scrape_id

    def get_scrape(self, scrape_id: str) -> ScrapeRecord | None:
        """Return a scrape dict, or ``None`` when it does not exist."""
        record = self._call(self._fetch_scrape(scrape_id))
        return self._record_to_scrape(record) if record else None

    async def _fetch_all_scrapes(self) -> list[ScrapeModel]:
        return (
            await ScrapeModel.all()
            .order_by("-created_order")
            .prefetch_related(*_SCRAP_CHILDREN)
        )

    def list_scrapes(self) -> list[ScrapeRecord]:
        """Return every scrape, newest first."""
        return [
            self._record_to_scrape(r) for r in self._call(self._fetch_all_scrapes())
        ]

    async def _write_status(self, scrape_id: str, status: str, error: str) -> None:
        await ScrapeModel.filter(id=scrape_id).update(
            status=status, error=error[:_ERROR_MAX_LENGTH]
        )

    def update_status(self, scrape_id: str, status: str, error: str = "") -> None:
        """Set a scrape's status and error text (truncated to 500 characters)."""
        self._call(self._write_status(scrape_id, status, error))
        self.publish()

    async def _write_progress(self, scrape_id: str, progress: ProgressData) -> None:
        values = _progress_values(progress)
        if values:
            await ScrapeModel.filter(id=scrape_id).update(**values)

    def save_progress(self, scrape_id: str, progress: ProgressData) -> None:
        """Persist a scrape's progress dict."""
        self._call(self._write_progress(scrape_id, progress))
        self.publish()

    async def _claim_oldest_pending(self) -> ScrapeModel | None:
        record = (
            await ScrapeModel.filter(status=ScrapeStatus.PENDING)
            .order_by("created_order")
            .prefetch_related(*_SCRAP_CHILDREN)
            .first()
        )
        if record:
            record.status = ScrapeStatus.WORKING
            await record.save(update_fields=["status"])
        return record

    def claim_next_pending(self) -> ScrapeRecord | None:
        """Mark the oldest pending scrape as working and return it, if any."""
        record = self._call(self._claim_oldest_pending())
        if not record:
            return None
        self.publish()
        return self._record_to_scrape(record)

    async def _requeue_interrupted(self, max_resumes: int) -> tuple[int, int]:
        resumed = failed = 0
        for record in await ScrapeModel.filter(status=ScrapeStatus.WORKING).all():
            if record.stage in ("", ScrapeStage.SEARCH):
                await ScrapeLeadModel.filter(
                    scrape_id=record.id, position__gte=record.rows_saved
                ).delete()
                await remove_websites_from(str(record.id), record.rows_saved)
            if record.resumes >= max_resumes:
                record.status = ScrapeStatus.FAILED
                record.error = (
                    f"the engine stopped while this scrape ran ({record.resumes + 1} times); "
                    "its rows so far are kept"
                )
                await record.save(update_fields=["status", "error"])
                failed += 1
            else:
                record.resumes += 1
                record.status = ScrapeStatus.PENDING
                await record.save(update_fields=["status", "resumes"])
                resumed += 1
        return resumed, failed

    def recover_interrupted(self, max_resumes: int = MAX_RESUMES) -> tuple[int, int]:
        """Requeue scrapes left ``working`` by a stopped engine.

        Returns:
            ``(resumed, failed)``: scrapes put back in the queue and scrapes that
            exceeded ``max_resumes`` and were marked failed.
        """
        return self._call(self._requeue_interrupted(max_resumes))

    async def _remove_scrape(self, scrape_id: str) -> int:
        await remove_scrape_websites(scrape_id)
        return await ScrapeModel.filter(id=scrape_id).delete()

    def delete_scrape(self, scrape_id: str) -> bool:
        """Delete a scrape; return whether it existed."""
        deleted = bool(self._call(self._remove_scrape(scrape_id)))
        if deleted:
            self.publish()
        return deleted

    # ── rows ──
    async def _scrape_exists(self, scrape_id: str) -> bool:
        return await ScrapeModel.filter(id=scrape_id).exists()

    async def _count_rows(self, scrape_id: str) -> int:
        return await ScrapeLeadModel.filter(scrape_id=scrape_id).count()

    @staticmethod
    def _make_row_records(
        scrape_id: str, rows: list[LeadRow], first_position: int
    ) -> list[ScrapeLeadModel]:
        return [
            ScrapeLeadModel(
                scrape_id=scrape_id,
                position=first_position + offset,
                **{
                    k: v
                    for k, v in row.items()
                    if k not in WEBSITE_FIELDS and k != "complete_address"
                },
            )
            for offset, row in enumerate(rows)
        ]

    async def _bulk_create_rows(
        self, scrape_id: str, rows: list[LeadRow], first_position: int
    ) -> None:
        await ScrapeLeadModel.bulk_create(
            self._make_row_records(scrape_id, rows, first_position)
        )
        end = first_position + len(rows)
        lead_ids = dict(
            await ScrapeLeadModel.filter(
                scrape_id=scrape_id, position__gte=first_position, position__lt=end
            ).values_list("position", "id")
        )
        addresses = [
            ScrapeAddressModel(lead_id=lead_ids[first_position + offset], **fields)
            for offset, row in enumerate(rows)
            if (fields := address_fields(row))
        ]
        await ScrapeAddressModel.bulk_create(addresses)

    async def _insert_rows(self, scrape_id: str, rows: list[LeadRow]) -> int | None:
        if not await self._scrape_exists(scrape_id):
            return None
        last = (
            await ScrapeLeadModel.filter(scrape_id=scrape_id)
            .order_by("-position")
            .first()
        )
        first_position = last.position + 1 if last else 0
        await self._bulk_create_rows(scrape_id, rows, first_position)
        return await self._count_rows(scrape_id)

    def append_rows(self, scrape_id: str, rows: list[LeadRow]) -> int | None:
        """Append rows and return the new row count, or ``None`` if the scrape is gone.

        Appending an empty list is a cheap way to read the current count.
        """
        return self._call(self._insert_rows(scrape_id, rows))

    async def _remove_rows_from(self, scrape_id: str, count: int) -> None:
        await ScrapeLeadModel.filter(
            scrape_id=scrape_id, position__gte=count or 0
        ).delete()
        await remove_websites_from(scrape_id, count)

    def truncate_rows(self, scrape_id: str, keep_count: int) -> None:
        """Delete every row at position ``keep_count`` or later."""
        self._call(self._remove_rows_from(scrape_id, keep_count))

    async def _replace_all_rows(
        self, scrape_id: str, rows: list[LeadRow]
    ) -> int | None:
        if not await self._scrape_exists(scrape_id):
            return None
        await ScrapeLeadModel.filter(scrape_id=scrape_id).delete()
        await self._bulk_create_rows(scrape_id, rows, 0)
        return len(rows)

    def replace_rows(self, scrape_id: str, rows: list[LeadRow]) -> int | None:
        """Replace all rows of a scrape; ``None`` if the scrape is gone."""
        return self._call(self._replace_all_rows(scrape_id, rows))

    async def _complete_scrape(
        self,
        scrape_id: str,
        rows: list[LeadRow],
        status: str,
        error: str,
        progress: ProgressData,
    ) -> bool:
        if await self._replace_all_rows(scrape_id, rows) is None:
            return False
        await ScrapeModel.filter(id=scrape_id).update(
            status=status,
            error=error[:_ERROR_MAX_LENGTH],
            **_progress_values(progress),
        )
        return True

    def finish_scrape(
        self,
        scrape_id: str,
        rows: list[LeadRow],
        status: str,
        error: str,
        progress: ProgressData,
    ) -> bool:
        """Store the final rows, status and progress; ``False`` if the scrape is gone."""
        completed = self._call(
            self._complete_scrape(scrape_id, rows, status, error, progress)
        )
        self.publish()
        return completed

    async def _select_rows(self, scrape_id: str) -> list[LeadRow]:
        records = (
            await ScrapeLeadModel.filter(scrape_id=scrape_id).order_by("position").all()
        )
        websites = await select_website_values(scrape_id)
        addresses = {
            address.lead_id: address
            for address in await ScrapeAddressModel.filter(lead__scrape_id=scrape_id)
        }
        rows: list[LeadRow] = []
        for record in records:
            row: dict[str, object] = dict(lead_of(record, addresses.get(record.id)))
            if record.position in websites:
                row.update(websites[record.position])
            rows.append(cast(LeadRow, row))
        return rows

    def read_rows(self, scrape_id: str) -> list[LeadRow]:
        """Return a scrape's rows in order."""
        return self._call(self._select_rows(scrape_id))
