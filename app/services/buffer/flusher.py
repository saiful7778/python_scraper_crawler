import asyncio
import threading
from collections.abc import Awaitable, Callable
from dataclasses import asdict
from functools import partial
from typing import Any

from tortoise.context import TortoiseContext
from tortoise.models import Model

from app.services.buffer.crawl_buffer import RedisCrawlBuffer
from app.services.buffer.crawl_types import WebsiteSnapshot
from app.services.buffer.row_buffer import RowBuffer
from app.services.buffer.scrape_state import ScrapeState
from app.services.google_map_scraper.scrape.scrape_types import LeadRow
from app.utils.errors import describe
from app.utils.logger import get_logger
from config.tortoise import TortoiseConfig

logger = get_logger(__name__)

FLUSH_SECONDS = 5
BATCH_SIZE = 500
#: Every context the engine opens uses this alias: Tortoise keeps each model's
#: default connection on the model class, so two aliases would override each other.
CONNECTION = "scrape_engine"
#: Columns an upsert never rewrites.
_KEPT_COLUMNS = frozenset({"id", "created_at"})

SaveRow = Callable[[LeadRow, str], Awaitable[None]]
#: Stores one finished website snapshot. It raises when the snapshot cannot be
#: stored yet; the flusher then keeps the website in Redis for the next tick.
CrawlSink = Callable[[WebsiteSnapshot], Awaitable[None]]


def context_config(
    config: TortoiseConfig, connection: str = CONNECTION
) -> dict[str, Any]:
    settings = asdict(config)
    (credentials,) = settings["connections"].values()
    settings["connections"] = {connection: credentials}
    for app in settings["apps"].values():
        app["default_connection"] = connection
        app.pop("migrations", None)
    return settings


def _upsert_fields(model: type[Model]) -> list[str]:
    return [
        column
        for column in model._meta.fields_db_projection
        if column not in _KEPT_COLUMNS
    ]


async def upsert(model: type[Model], rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    await model.bulk_create(
        [model(**row) for row in rows],
        batch_size=BATCH_SIZE,
        on_conflict=["id"],
        update_fields=_upsert_fields(model),
    )


class Flusher(threading.Thread):
    """Moves buffered lead rows from Redis into PostgreSQL.

    Rows leave Redis only after they are saved, and a scrape's rows are saved in
    order, so a failure keeps the failed row and everything after it for the
    next tick. ``config`` makes the flusher open its own Tortoise connection on
    its event loop; without it, ``save_row`` runs in whatever Tortoise context
    the caller already has.

    After the rows, each finished crawl in ``crawl_buffer`` goes through
    ``crawl_sink`` and is released from Redis once the sink accepted it.
    """

    def __init__(
        self,
        rows: RowBuffer,
        state: ScrapeState,
        save_row: SaveRow,
        crawl_sink: CrawlSink | None,
        interval: float = FLUSH_SECONDS,
        config: TortoiseConfig | None = None,
        crawl_buffer: RedisCrawlBuffer | None = None,
    ):
        super().__init__(name="buffer-flusher", daemon=True)
        self._rows = rows
        self._state = state
        self._save_row = save_row
        self._crawl_sink = crawl_sink
        self._crawl_buffer = crawl_buffer
        self._interval = interval
        self._config = config
        self._context: TortoiseContext | None = None
        self._loop = asyncio.new_event_loop()
        self._stop_requested = threading.Event()

    def flush_once(self) -> int:
        saved = 0
        for scrape_id in self._state.active_ids():
            batch = self._rows.peek(scrape_id, BATCH_SIZE)
            if not batch:
                continue
            self._state.refresh_ttl(scrape_id)
            count = self._loop.run_until_complete(self._save_batch(batch, scrape_id))
            if count:
                self._rows.trim(scrape_id, count)
            saved += count
        self._flush_crawls()
        return saved

    def run(self) -> None:
        try:
            while not self._stop_requested.wait(self._interval):
                self._tick()
            self._tick()
        finally:
            self._close()

    def shutdown(self, wait: float = 0) -> None:
        self._stop_requested.set()
        if self.is_alive():
            if wait > 0:
                self.join(wait)
        elif self.ident is None:
            self._close()

    def _tick(self) -> None:
        try:
            self.flush_once()
        except Exception as error:
            logger.error(
                "flushing buffered rows failed: %s", describe(error), exc_info=error
            )

    def _flush_crawls(self) -> None:
        crawl_buffer, sink = self._crawl_buffer, self._crawl_sink
        if crawl_buffer is None or sink is None:
            return
        for website_id in crawl_buffer.finished_website_ids():
            try:
                snapshot = crawl_buffer.read_website(website_id)
            except LookupError as error:
                logger.warning(
                    "crawl of website %s is gone from Redis, so it is skipped: %s",
                    website_id,
                    describe(error),
                )
                crawl_buffer.release_website(website_id)
                continue
            try:
                self._loop.run_until_complete(
                    self._in_context(partial(sink, snapshot))
                )
            except Exception as error:
                logger.error(
                    "saving the crawl of website %s failed; it stays in Redis: %s",
                    website_id,
                    describe(error),
                    exc_info=error,
                )
                continue
            crawl_buffer.release_website(website_id)

    async def _save_batch(self, batch: list[LeadRow], scrape_id: str) -> int:
        saved = 0
        for row in batch:
            try:
                await self._save_in_context(row, scrape_id)
            except Exception as error:
                logger.error(
                    "saving a buffered row of scrape %s failed; it stays in Redis: %s",
                    scrape_id,
                    describe(error),
                    exc_info=error,
                )
                break
            saved += 1
        return saved

    async def _save_in_context(self, row: LeadRow, scrape_id: str) -> None:
        await self._in_context(lambda: self._save_row(row, scrape_id))

    async def _in_context(self, work: Callable[[], Awaitable[None]]) -> None:
        if self._config is None:
            await work()
            return
        from tortoise.context import _current_context

        context = await self._open(self._config)
        token = _current_context.set(context)
        try:
            await work()
        finally:
            _current_context.reset(token)

    async def _open(self, config: TortoiseConfig) -> TortoiseContext:
        if self._context is None:
            context = TortoiseContext()
            context.__enter__()
            try:
                await context.init(config=context_config(config))
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

    def _close(self) -> None:
        if self._loop.is_closed():
            return
        try:
            if self._context is not None:
                self._loop.run_until_complete(self._context.close_connections())
        except Exception as error:  # noqa: BLE001 -- shutting down; nothing left to keep
            logger.warning(
                "closing the PostgreSQL connection failed: %s", describe(error)
            )
        finally:
            self._loop.close()
