"""A blocking view of the PostgreSQL scrape records, for the threads of the engine.

Tortoise is asynchronous, so the database runs its own event loop on a daemon
thread, with its own Tortoise context and connection, and every call waits for
its result. The connection opens on the first call.
"""

import asyncio
import threading
from collections.abc import Coroutine
from typing import TypeVar
from uuid import uuid4

from tortoise.context import TortoiseContext

from app.repositories.scrape_repository import ScrapeRepository
from app.services.buffer.flusher import context_config
from app.utils.errors import ScraperError, describe
from app.utils.logger import get_logger
from config.tortoise import TortoiseConfig

from .scrape_status import ScrapeStatus
from .scrape_types import ProgressData, ScrapeRecord, SettingsData

logger = get_logger(__name__)

T = TypeVar("T")

_CLOSE_TIMEOUT_SECONDS = 5


class ScrapeDatabaseError(ScraperError):
    """PostgreSQL failed while the engine read or wrote scrape records."""


class ScrapeDatabase:
    """Reads and writes the ``scrapes`` table; each call blocks until PostgreSQL answers."""

    def __init__(self, config: TortoiseConfig):
        self._config = config
        self._repository = ScrapeRepository()
        self._context: TortoiseContext | None = None
        self._closed = False
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._serve_loop, name="scrape-postgres", daemon=True
        )
        self._thread.start()

    def create(self, name: str, settings: SettingsData) -> str:
        """Insert a pending scrape and return its id."""
        scrape_id = uuid4()
        self._call(
            "creating the scrape",
            self._repository.create(
                scrape_id, name, settings, list(settings["keywords"])
            ),
        )
        return str(scrape_id)

    def get(self, scrape_id: str) -> ScrapeRecord | None:
        return self._call("reading the scrape", self._repository.get(scrape_id))

    def claim_next_pending(self) -> ScrapeRecord | None:
        """Mark the oldest pending scrape as working and return it."""
        return self._call(
            "claiming a scrape", self._repository.claim_oldest_pending()
        )

    def set_status(self, scrape_id: str, status: str, error: str = "") -> None:
        self._call(
            "saving the scrape status",
            self._repository.set_status(scrape_id, status, error),
        )

    def finish(
        self, scrape_id: str, status: str, error: str, progress: ProgressData
    ) -> bool:
        """Store the final status; False when the scrape was deleted."""
        return self._call(
            "finishing the scrape",
            self._repository.finish(scrape_id, status, error, progress),
        )

    def requeue_interrupted(self, max_resumes: int) -> tuple[int, int]:
        """Resume scrapes a stopped engine left working; returns ``(resumed, failed)``."""
        return self._call(
            "resuming interrupted scrapes",
            self._repository.requeue_interrupted(max_resumes),
        )

    def pending_ids(self) -> list[str]:
        """The ids of the scrapes waiting to run, oldest first."""
        records = self._call("listing the scrapes", self._repository.list_all())
        return [
            record["ID"]
            for record in reversed(records)
            if record["Status"] == ScrapeStatus.PENDING
        ]

    def close(self) -> None:
        """Stop the loop thread; calling it twice is harmless."""
        if self._closed:
            return
        self._closed = True
        try:
            self._loop.call_soon_threadsafe(self._loop.stop)
        except RuntimeError as error:
            logger.warning("the scrape database loop was already closed: %s", describe(error))
        self._thread.join(_CLOSE_TIMEOUT_SECONDS)
        if self._thread.is_alive():
            logger.warning(
                "the scrape database thread did not stop within %s s",
                _CLOSE_TIMEOUT_SECONDS,
            )

    def _call(self, what: str, coroutine: Coroutine[object, object, T]) -> T:
        if self._closed or not self._thread.is_alive():
            coroutine.close()
            raise ScrapeDatabaseError(
                f"PostgreSQL failed while {what}: the scrape database is closed"
            )
        try:
            return asyncio.run_coroutine_threadsafe(
                self._in_context(coroutine), self._loop
            ).result()
        except Exception as error:
            raise ScrapeDatabaseError(
                f"PostgreSQL failed while {what}: {describe(error)}"
            ) from error

    def _serve_loop(self) -> None:
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_forever()
            if self._context is not None:
                self._loop.run_until_complete(self._context.close_connections())
        except Exception:
            logger.exception("closing the scrape database connection failed")
        finally:
            self._loop.close()

    async def _open(self) -> TortoiseContext:
        if self._context is None:
            context = TortoiseContext()
            context.__enter__()
            try:
                await context.init(config=context_config(self._config))
            except BaseException:
                try:
                    await context.close_connections()
                except Exception as close_error:  # noqa: BLE001 -- the connect error is the one to report
                    logger.warning(
                        "closing the failed PostgreSQL connection also failed: %s",
                        describe(close_error),
                    )
                raise
            finally:
                context.__exit__(None, None, None)
            self._context = context
        return self._context

    async def _in_context(self, coroutine: Coroutine[object, object, T]) -> T:
        from tortoise.context import _current_context

        try:
            context = await self._open()
        except BaseException:
            coroutine.close()
            raise
        token = _current_context.set(context)
        try:
            return await coroutine
        finally:
            _current_context.reset(token)
