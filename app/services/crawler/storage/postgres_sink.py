"""Copies a finished website from the SQLite crawl buffer to Postgres.

Like :class:`ScrapeStore`, the sink runs a private asyncio loop on a daemon
thread with its own Tortoise context, and exposes a blocking API.  The
connection is opened on the first copy, so a database that is down at startup
does not stop the engine.
"""

import asyncio
import os
import threading
from collections.abc import Coroutine
from dataclasses import asdict
from typing import Any, TypeVar
from uuid import NAMESPACE_URL, UUID, uuid5

from tortoise.context import TortoiseContext
from tortoise.models import Model
from tortoise.transactions import in_transaction

from app.models import (
    CrawlEventModel,
    CrawlFindingModel,
    CrawlLinkModel,
    CrawlPageModel,
    CrawlRawHTMLModel,
    CrawlRunModel,
    CrawlWebsiteModel,
)
from app.utils.errors import ConfigError, describe
from app.utils.logger import get_logger
from config.tortoise import TortoiseConfig

from .buffer import WebsiteSnapshot

logger = get_logger(__name__)

T = TypeVar("T")

_BATCH_SIZE = 500
#: Seconds to wait for the sink thread to stop on close.
_CLOSE_TIMEOUT_SECONDS = 5
#: Connection name of the sink. Tortoise caches insert statements by connection
#: name and table, and the SQLite buffer uses the same table names on "default".
_CONNECTION = "crawl_postgres"
#: Columns an upsert never rewrites.
_KEPT_COLUMNS = frozenset({"id", "created_at", "stored_at"})


def _context_config(config: TortoiseConfig) -> dict[str, Any]:
    settings = asdict(config)
    (credentials,) = settings["connections"].values()
    settings["connections"] = {_CONNECTION: credentials}
    for app in settings["apps"].values():
        app["default_connection"] = _CONNECTION
        app.pop("migrations", None)
    return settings


def event_id(run_id: object, sqlite_event_id: object) -> UUID:
    """Postgres id of an event: stable per run, and distinct across SQLite resets."""
    return uuid5(NAMESPACE_URL, f"{run_id}:{sqlite_event_id}")


def _event_rows(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{**event, "id": event_id(event["run_id"], event["id"])} for event in events]


def _upsert_fields(model: type[Model]) -> list[str]:
    return [
        column
        for column in model._meta.fields_db_projection
        if column not in _KEPT_COLUMNS
    ]


async def _upsert(model: type[Model], rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    await model.bulk_create(
        [model(**row) for row in rows],
        batch_size=_BATCH_SIZE,
        on_conflict=["id"],
        update_fields=_upsert_fields(model),
    )


class PostgresSink:
    """Blocking facade that upserts a :class:`WebsiteSnapshot` into Postgres."""

    def __init__(self, config: TortoiseConfig | None):
        """Build a sink for ``config``; ``None`` disables it."""
        self._config = config
        self._closed = False
        self._context: TortoiseContext | None = None
        if config is None:
            return
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._serve_loop, name="crawl-postgres", daemon=True
        )
        self._thread.start()

    @classmethod
    def from_environment(cls) -> "PostgresSink":
        """Build a sink from the ``DB_*`` variables; disabled unless ``DB_HOST`` is set.

        Raises:
            ConfigError: the ``DB_*`` variables hold values that cannot be used.
        """
        if not os.getenv("DB_HOST"):
            logger.info("DB_HOST is not set; finished crawls stay in the local buffer")
            return cls(None)
        try:
            config = TortoiseConfig()
        except Exception as error:
            logger.error(
                "the DB_* variables are invalid, so Postgres cannot be used: %s",
                describe(error),
                exc_info=error,
            )
            raise ConfigError(
                f"invalid DB_* settings for the Postgres sink: {describe(error)}"
            ) from error
        return cls(config)

    @property
    def enabled(self) -> bool:
        """Whether copies are written anywhere."""
        return self._config is not None

    def _serve_loop(self) -> None:
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_forever()
            if self._context is not None:
                self._loop.run_until_complete(self._context.close_connections())
        except Exception:
            logger.exception("closing the Postgres connections failed")
        finally:
            self._loop.close()

    async def _open(self) -> TortoiseContext:
        if self._context is None:
            if self._config is None:
                raise RuntimeError("the Postgres sink is disabled")
            context = TortoiseContext()
            context.__enter__()
            try:
                await context.init(config=_context_config(self._config))
            except BaseException as error:
                logger.error(
                    "could not connect to Postgres: %s", describe(error), exc_info=error
                )
                try:
                    await context.close_connections()
                except Exception as close_error:  # noqa: BLE001 -- the connect error is the one to report
                    logger.warning(
                        "closing the failed Postgres connection also failed: %s",
                        describe(close_error),
                    )
                raise
            finally:
                context.__exit__(None, None, None)
            self._context = context
        return self._context

    async def _run_in_context(self, coroutine: Coroutine[object, object, T]) -> T:
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

    def run_async(self, coroutine: Coroutine[object, object, T]) -> T:
        """Run a coroutine on the sink loop and wait for its result."""
        if self._closed or not self.enabled or not self._thread.is_alive():
            coroutine.close()
            logger.error(
                "a Postgres operation was refused: the sink is closed, disabled or its thread has stopped"
            )
            raise RuntimeError("the Postgres sink is not running")
        return asyncio.run_coroutine_threadsafe(
            self._run_in_context(coroutine), self._loop
        ).result()

    async def _copy(self, snapshot: WebsiteSnapshot) -> None:
        website = {**snapshot.website, "scrape_id": UUID(str(snapshot.website["scrape_id"]))}
        async with in_transaction():
            await _upsert(CrawlWebsiteModel, [website])
            await _upsert(CrawlRunModel, snapshot.runs)
            await _upsert(CrawlPageModel, snapshot.pages)
            await _upsert(CrawlLinkModel, snapshot.links)
            await _upsert(CrawlRawHTMLModel, snapshot.raw_html)
            await _upsert(CrawlFindingModel, snapshot.findings)
            await _upsert(CrawlEventModel, _event_rows(snapshot.events))

    def copy_website(self, snapshot: WebsiteSnapshot) -> None:
        """Write a website and everything recorded for it; repeating it is harmless.

        Raises:
            Exception: whatever Postgres or the connection raised; nothing is
                written in that case.
        """
        if not self.enabled:
            return
        self.run_async(self._copy(snapshot))

    def close(self) -> None:
        """Stop the loop thread; calling it twice is harmless."""
        if self._closed or not self.enabled:
            return
        self._closed = True
        try:
            self._loop.call_soon_threadsafe(self._loop.stop)
        except RuntimeError as error:
            logger.warning(
                "the Postgres sink loop was already closed: %s", describe(error)
            )
        self._thread.join(_CLOSE_TIMEOUT_SECONDS)
        if self._thread.is_alive():
            logger.warning(
                "the Postgres sink thread did not stop within %s s",
                _CLOSE_TIMEOUT_SECONDS,
            )
