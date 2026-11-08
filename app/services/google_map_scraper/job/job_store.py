"""SQLite-backed storage of jobs and their result rows.

Tortoise ORM is asynchronous, so :class:`JobStore` runs a private asyncio loop
on a daemon thread and exposes a plain synchronous API to the rest of the
engine.  Every mutation publishes a fresh snapshot to the subscribers (the
live job list shown in the UI).
"""

import asyncio
import logging
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

from app.utils.errors import ScraperError, describe

from .job_models import (
    SCHEMA_VERSION,
    JobKeywordModel,
    JobLeadModel,
    JobModel,
    JobProxyModel,
    JobSettingsModel,
    lead_of,
    settings_columns,
    settings_of,
)
from .job_presenter import to_public_job
from .job_status import JobStage, JobStatus
from .job_types import JobRecord, LeadRow, ProgressData, PublicJob, SettingsData

logger = logging.getLogger(__name__)

_ERROR_MAX_LENGTH = 500
#: Seconds the starting thread waits for the database to open.
_STARTUP_TIMEOUT_SECONDS = 60
#: Seconds between checks that the database thread is still alive during a call.
_CALL_POLL_SECONDS = 1.0
#: Seconds to wait for the database thread to stop on close.
_CLOSE_TIMEOUT_SECONDS = 5
#: Related rows a job record is built from.
_JOB_CHILDREN = ("settings_row", "keywords", "proxies")


class JobStoreError(ScraperError):
    """The job database could not be opened, or the thread serving it stopped."""


MAX_RESUMES = (
    3  # restarts of the engine mid-job before the job is failed instead of resumed
)

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
    """Object exposed as ``JobStore.db`` so callers can close the store uniformly."""

    def __init__(self, store: "JobStore"):
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


def _progress_of(record: JobModel) -> ProgressData:
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
    """Return the path of the job database inside ``directory``."""
    return os.path.join(directory, "jobs.db")


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
            "job database %s has schema version %s, expected %s; deleting it and starting empty",
            path,
            version,
            SCHEMA_VERSION,
        )
        os.remove(path)


class JobStore:
    """Synchronous facade over the job tables, with change notifications."""

    def __init__(self, directory: str):
        """Open (creating if needed) ``jobs.db`` inside ``directory``.

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
        self._listeners: set[queue.Queue[list[PublicJob]]] = set()
        self._listeners_lock = threading.Lock()
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._serve_loop, name="engine-db", daemon=True
        )
        self._thread.start()
        if not self._ready.wait(_STARTUP_TIMEOUT_SECONDS):
            raise JobStoreError(
                f"the job database at {database_path(directory)} did not open within "
                f"{_STARTUP_TIMEOUT_SECONDS} s (is another process locking it?)"
            )
        if self._startup_error:
            raise JobStoreError(
                f"could not open the job database at {database_path(directory)}: "
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
            logger.error("closing the job database connections failed", exc_info=True)
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
            modules={"engine_jobs": ["app.services.google_map_scraper.job.job_models"]},
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

        :raises JobStoreError: If the store is closed or its thread stopped
            while the call was waiting.
        """
        if self._closed:
            raise JobStoreError("the job store is closed; the call was not run")
        if not self._thread.is_alive():
            raise JobStoreError(
                "the job database thread has stopped; the call was not run"
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
                    raise JobStoreError(
                        "the job database thread stopped while a call was waiting"
                    ) from None

    def close(self) -> None:
        """Stop the loop thread; calling it twice is harmless."""
        if self._closed:
            return
        self._closed = True
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(_CLOSE_TIMEOUT_SECONDS)
        if self._thread.is_alive():
            logger.warning(
                "the job database thread did not stop within %s s; pending writes may be lost",
                _CLOSE_TIMEOUT_SECONDS,
            )

    # ── change notifications ──
    def subscribe(self) -> "queue.Queue[list[PublicJob]]":
        """Register and return a queue that receives job-list snapshots."""
        listener: queue.Queue[list[PublicJob]] = queue.Queue()
        with self._listeners_lock:
            self._listeners.add(listener)
        return listener

    def unsubscribe(self, listener: "queue.Queue[list[PublicJob]]") -> None:
        """Stop sending snapshots to ``listener``."""
        with self._listeners_lock:
            self._listeners.discard(listener)

    def publish(self) -> None:
        """Send the current public job list to every subscriber."""
        with self._listeners_lock:
            listeners = tuple(self._listeners)
        if not listeners:
            return
        snapshot = [to_public_job(job) for job in self.list_jobs()]
        for listener in listeners:
            listener.put(snapshot)

    # ── records ──
    @staticmethod
    def _record_to_job(record: JobModel) -> JobRecord:
        """Convert a ``JobModel`` to the job dict used across the engine."""
        return {
            "ID": str(record.id),
            "Name": record.name,
            "Date": record.created_at.isoformat(timespec="seconds"),
            "Status": record.status,
            "Data": settings_of(record),
            "Error": record.error,
            "Progress": _progress_of(record),
        }

    async def _fetch_job(self, job_id: str) -> JobModel | None:
        return await JobModel.get_or_none(id=job_id).prefetch_related(*_JOB_CHILDREN)

    async def _insert_job(
        self, job_id: str, name: str, settings_data: SettingsData
    ) -> JobModel:
        last = await JobModel.all().order_by("-created_order").first()
        async with in_transaction():
            job = await JobModel.create(
                id=job_id,
                name=name,
                status=JobStatus.PENDING,
                created_order=(last.created_order + 1) if last else 1,
            )
            await JobSettingsModel.create(
                job=job,
                **cast(dict[str, Any], settings_columns(settings_data)),
            )
            await JobKeywordModel.bulk_create(
                JobKeywordModel(job=job, position=position, term=term)
                for position, term in enumerate(settings_data["keywords"])
            )
            await JobProxyModel.bulk_create(
                JobProxyModel(job=job, position=position, url=url)
                for position, url in enumerate(settings_data.get("proxies", []))
            )
        return job

    def create_job(self, name: str, settings_data: SettingsData) -> str:
        """Queue a new job and return its id."""
        job_id = str(uuid.uuid4())
        self._call(self._insert_job(job_id, name, settings_data))
        self.publish()
        return job_id

    def get_job(self, job_id: str) -> JobRecord | None:
        """Return a job dict, or ``None`` when it does not exist."""
        record = self._call(self._fetch_job(job_id))
        return self._record_to_job(record) if record else None

    async def _fetch_all_jobs(self) -> list[JobModel]:
        return (
            await JobModel.all()
            .order_by("-created_order")
            .prefetch_related(*_JOB_CHILDREN)
        )

    def list_jobs(self) -> list[JobRecord]:
        """Return every job, newest first."""
        return [self._record_to_job(r) for r in self._call(self._fetch_all_jobs())]

    async def _write_status(self, job_id: str, status: str, error: str) -> None:
        await JobModel.filter(id=job_id).update(
            status=status, error=error[:_ERROR_MAX_LENGTH]
        )

    def update_status(self, job_id: str, status: str, error: str = "") -> None:
        """Set a job's status and error text (truncated to 500 characters)."""
        self._call(self._write_status(job_id, status, error))
        self.publish()

    async def _write_progress(self, job_id: str, progress: ProgressData) -> None:
        values = _progress_values(progress)
        if values:
            await JobModel.filter(id=job_id).update(**values)

    def save_progress(self, job_id: str, progress: ProgressData) -> None:
        """Persist a job's progress dict."""
        self._call(self._write_progress(job_id, progress))
        self.publish()

    async def _claim_oldest_pending(self) -> JobModel | None:
        record = (
            await JobModel.filter(status=JobStatus.PENDING)
            .order_by("created_order")
            .prefetch_related(*_JOB_CHILDREN)
            .first()
        )
        if record:
            record.status = JobStatus.WORKING
            await record.save(update_fields=["status"])
        return record

    def claim_next_pending(self) -> JobRecord | None:
        """Mark the oldest pending job as working and return it, if any."""
        record = self._call(self._claim_oldest_pending())
        if not record:
            return None
        self.publish()
        return self._record_to_job(record)

    async def _requeue_interrupted(self, max_resumes: int) -> tuple[int, int]:
        resumed = failed = 0
        for record in await JobModel.filter(status=JobStatus.WORKING).all():
            if record.stage in ("", JobStage.SEARCH):
                await JobLeadModel.filter(
                    job_id=record.id, position__gte=record.rows_saved
                ).delete()
            if record.resumes >= max_resumes:
                record.status = JobStatus.FAILED
                record.error = (
                    f"the engine stopped while this job ran ({record.resumes + 1} times); "
                    "its rows so far are kept"
                )
                await record.save(update_fields=["status", "error"])
                failed += 1
            else:
                record.resumes += 1
                record.status = JobStatus.PENDING
                await record.save(update_fields=["status", "resumes"])
                resumed += 1
        return resumed, failed

    def recover_interrupted(self, max_resumes: int = MAX_RESUMES) -> tuple[int, int]:
        """Requeue jobs left ``working`` by a stopped engine.

        Returns:
            ``(resumed, failed)``: jobs put back in the queue and jobs that
            exceeded ``max_resumes`` and were marked failed.
        """
        return self._call(self._requeue_interrupted(max_resumes))

    async def _remove_job(self, job_id: str) -> int:
        return await JobModel.filter(id=job_id).delete()

    def delete_job(self, job_id: str) -> bool:
        """Delete a job; return whether it existed."""
        deleted = bool(self._call(self._remove_job(job_id)))
        if deleted:
            self.publish()
        return deleted

    # ── rows ──
    async def _job_exists(self, job_id: str) -> bool:
        return await JobModel.filter(id=job_id).exists()

    async def _count_rows(self, job_id: str) -> int:
        return await JobLeadModel.filter(job_id=job_id).count()

    @staticmethod
    def _make_row_records(
        job_id: str, rows: list[LeadRow], first_position: int
    ) -> list[JobLeadModel]:
        return [
            JobLeadModel(job_id=job_id, position=first_position + offset, **row)
            for offset, row in enumerate(rows)
        ]

    async def _insert_rows(self, job_id: str, rows: list[LeadRow]) -> int | None:
        if not await self._job_exists(job_id):
            return None
        last = await JobLeadModel.filter(job_id=job_id).order_by("-position").first()
        first_position = last.position + 1 if last else 0
        await JobLeadModel.bulk_create(
            self._make_row_records(job_id, rows, first_position)
        )
        return await self._count_rows(job_id)

    def append_rows(self, job_id: str, rows: list[LeadRow]) -> int | None:
        """Append rows and return the new row count, or ``None`` if the job is gone.

        Appending an empty list is a cheap way to read the current count.
        """
        return self._call(self._insert_rows(job_id, rows))

    async def _remove_rows_from(self, job_id: str, count: int) -> None:
        await JobLeadModel.filter(job_id=job_id, position__gte=count or 0).delete()

    def truncate_rows(self, job_id: str, keep_count: int) -> None:
        """Delete every row at position ``keep_count`` or later."""
        self._call(self._remove_rows_from(job_id, keep_count))

    async def _replace_all_rows(self, job_id: str, rows: list[LeadRow]) -> int | None:
        if not await self._job_exists(job_id):
            return None
        await JobLeadModel.filter(job_id=job_id).delete()
        await JobLeadModel.bulk_create(self._make_row_records(job_id, rows, 0))
        return len(rows)

    def replace_rows(self, job_id: str, rows: list[LeadRow]) -> int | None:
        """Replace all rows of a job; ``None`` if the job is gone."""
        return self._call(self._replace_all_rows(job_id, rows))

    async def _complete_job(
        self,
        job_id: str,
        rows: list[LeadRow],
        status: str,
        error: str,
        progress: ProgressData,
    ) -> bool:
        if await self._replace_all_rows(job_id, rows) is None:
            return False
        await JobModel.filter(id=job_id).update(
            status=status,
            error=error[:_ERROR_MAX_LENGTH],
            **_progress_values(progress),
        )
        return True

    def finish_job(
        self,
        job_id: str,
        rows: list[LeadRow],
        status: str,
        error: str,
        progress: ProgressData,
    ) -> bool:
        """Store the final rows, status and progress; ``False`` if the job is gone."""
        completed = self._call(
            self._complete_job(job_id, rows, status, error, progress)
        )
        self.publish()
        return completed

    async def _select_rows(self, job_id: str) -> list[LeadRow]:
        records = await JobLeadModel.filter(job_id=job_id).order_by("position").all()
        return [lead_of(record) for record in records]

    def read_rows(self, job_id: str) -> list[LeadRow]:
        """Return a job's rows in order."""
        return self._call(self._select_rows(job_id))
