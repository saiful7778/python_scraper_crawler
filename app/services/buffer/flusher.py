import asyncio
import threading
import time
from collections.abc import Awaitable, Callable
from functools import partial
from typing import Any

from tortoise.models import Model

from app.services.buffer.crawl_buffer import RedisCrawlBuffer
from app.services.buffer.crawl_types import WebsiteSnapshot
from app.services.buffer.row_buffer import RowBuffer
from app.services.buffer.scrape_state import ScrapeState
from app.services.google_map_scraper.scrape.scrape_types import LeadRow
from app.services.postgres import CONNECTION_LOST, TortoiseConnection
from app.utils.errors import describe
from app.utils.logger import get_logger
from config.tortoise import TortoiseConfig

logger = get_logger(__name__)

BATCH_SIZE = 500
#: Columns an upsert never rewrites.
_KEPT_COLUMNS = frozenset({"id", "created_at"})

SaveRow = Callable[[LeadRow, str], Awaitable[None]]
#: Saves rows in order and returns how many were saved from the start.
SaveRows = Callable[[list[LeadRow], str], Awaitable[int]]
#: Stores one finished website snapshot. It raises when the snapshot cannot be
#: stored yet; the flusher then keeps the website in Redis for the next save.
CrawlSink = Callable[[WebsiteSnapshot], Awaitable[None]]


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
    next save. ``config`` makes the flusher open its own Tortoise connection on
    its event loop; without it, ``save_row`` runs in whatever Tortoise context
    the caller already has.

    There is no periodic save: a queue job that finishes calls ``flush_scrape``
    (a scrape's rows) or ``flush_crawls`` (finished website crawls), so rows are
    written when their job ends. ``flush_once`` saves everything, which is what
    the thread does once when it stops, so a stop never loses a finished job.

    After the rows, each finished crawl in ``crawl_buffer`` goes through
    ``crawl_sink`` and is released from Redis once the sink accepted it.
    """

    def __init__(
        self,
        rows: RowBuffer,
        state: ScrapeState,
        save_row: SaveRow,
        crawl_sink: CrawlSink | None,
        config: TortoiseConfig | None = None,
        crawl_buffer: RedisCrawlBuffer | None = None,
        prepare_batch: Callable[[list[LeadRow]], None] | None = None,
        save_rows: SaveRows | None = None,
    ):
        super().__init__(name="buffer-flusher", daemon=True)
        self._rows = rows
        self._state = state
        self._save_row = save_row
        self._save_rows = save_rows
        self._prepare_batch = prepare_batch
        self._crawl_sink = crawl_sink
        self._crawl_buffer = crawl_buffer
        self._connection = TortoiseConnection(config) if config is not None else None
        self._loop = asyncio.new_event_loop()
        self._stop_requested = threading.Event()
        #: One loop and one connection serve every caller, so a scrape is saved by
        #: one thread at a time: the flusher thread at its final save, or a queue
        #: job finishing its rows.
        self._lock = threading.Lock()

    def flush_crawls(self) -> None:
        """Store every finished crawl now, and release the ones the sink accepted.

        The crawl stage calls this when a website's job finishes, so its crawl is
        written then rather than on a timer.
        """
        with self._lock:
            self._flush_crawls()

    def flush_once(self) -> int:
        saved = 0
        for scrape_id in self._state.active_ids():
            saved += self._flush_scrape(scrape_id)
        with self._lock:
            self._flush_crawls()
        if saved:
            logger.debug("flushed %d buffered lead row(s)", saved)
        return saved

    def flush_scrape(self, scrape_id: str) -> int:
        """Save every buffered row of one scrape now; returns how many were saved.

        A queue job finishing its tile calls this, so its rows are written when
        the job ends. A row that fails stops the save and stays in Redis with the
        rows after it.
        """
        saved = 0
        while True:
            count = self._flush_scrape(scrape_id)
            saved += count
            if count < BATCH_SIZE:
                return saved

    def _flush_scrape(self, scrape_id: str) -> int:
        with self._lock:
            batch = self._rows.peek(scrape_id, BATCH_SIZE)
            if not batch:
                return 0
            self._state.refresh_ttl(scrape_id)
            if not self._connection_ok():
                return 0
            if self._prepare_batch is not None:
                prepare_started = time.monotonic()
                self._prepare_batch(batch)
                logger.info(
                    "flusher: prepared %d buffered row(s) in %.2f s",
                    len(batch),
                    time.monotonic() - prepare_started,
                )
            save_started = time.monotonic()
            count = self._loop.run_until_complete(self._save_batch(batch, scrape_id))
            logger.info(
                "flusher: saved %d of %d buffered row(s) for scrape %s in %.2f s",
                count,
                len(batch),
                scrape_id,
                time.monotonic() - save_started,
            )
            if count:
                self._rows.trim(scrape_id, count)
            return count

    def run(self) -> None:
        """Wait for the stop, then save what the last jobs left behind."""
        try:
            self._stop_requested.wait()
            self._save_and_close()
        except BaseException:
            self._close()
            raise

    def _save_and_close(self) -> None:
        try:
            self.flush_once()
        except Exception as error:
            logger.error(
                "flushing buffered rows failed: %s", describe(error), exc_info=error
            )
        finally:
            self._close()

    def shutdown(self, wait: float | None = 0) -> None:
        """Stop the flusher; ``None`` waits for the final save with no time limit."""
        self._stop_requested.set()
        if self.is_alive():
            if wait is None:
                self.join()
            elif wait > 0:
                self.join(wait)
        elif self.ident is None:
            self._close()

    def _flush_crawls(self) -> None:
        crawl_buffer, sink = self._crawl_buffer, self._crawl_sink
        if crawl_buffer is None or sink is None:
            return
        website_ids = crawl_buffer.finished_website_ids()
        if not website_ids:
            return
        if not self._connection_ok():
            return
        for website_id in website_ids:
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
                self._loop.run_until_complete(self._in_context(partial(sink, snapshot)))
            except Exception as error:
                logger.error(
                    "saving the crawl of website %s failed; it stays in Redis: %s",
                    website_id,
                    describe(error),
                    exc_info=error,
                )
                continue
            logger.info("crawl of website %s stored in PostgreSQL", website_id)
            crawl_buffer.release_website(website_id)

    def _connection_ok(self) -> bool:
        """True if the flusher's own connection answers; a dead one is replaced once."""
        if self._connection is None:
            return True
        try:
            self._loop.run_until_complete(self._connection.ensure())
        except Exception as error:
            logger.error(
                "the PostgreSQL connection is not usable; buffered rows stay in Redis: %s",
                describe(error),
                exc_info=error,
            )
            return False
        return True

    async def _save_batch(self, batch: list[LeadRow], scrape_id: str) -> int:
        if self._save_rows is not None:
            return await self._save_batch_at_once(batch, scrape_id)
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

    async def _save_batch_at_once(self, batch: list[LeadRow], scrape_id: str) -> int:
        save_rows = self._save_rows
        assert save_rows is not None
        try:
            return await self._in_context(lambda: save_rows(batch, scrape_id))
        except CONNECTION_LOST:
            if self._connection is not None:
                await self._connection.drop()
            return await self._in_context(lambda: save_rows(batch, scrape_id))

    async def _save_in_context(self, row: LeadRow, scrape_id: str) -> None:
        try:
            await self._in_context(lambda: self._save_row(row, scrape_id))
        except CONNECTION_LOST:
            if self._connection is not None:
                await self._connection.drop()
            await self._in_context(lambda: self._save_row(row, scrape_id))

    async def _in_context[T](self, work: Callable[[], Awaitable[T]]) -> T:
        if self._connection is None:
            return await work()
        from tortoise.context import _current_context

        context = await self._connection.open()
        token = _current_context.set(context)
        try:
            return await work()
        finally:
            _current_context.reset(token)

    def _close(self) -> None:
        if self._loop.is_closed():
            return
        try:
            if self._connection is not None:
                self._loop.run_until_complete(self._connection.close())
        except Exception as error:  # noqa: BLE001 -- shutting down; nothing left to keep
            logger.warning(
                "closing the PostgreSQL connection failed: %s", describe(error)
            )
        finally:
            self._loop.close()
