"""Drain a plan's pages across the API keys, each at its own pace."""

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from app.repositories.serper_map_repository import SerperMapPage
from app.utils.errors import describe
from app.utils.logger import get_logger

from .serper_map_client import SerperMapClient
from .serper_map_errors import (
    SerperAuthError,
    SerperConfigError,
    SerperNoCreditsError,
    SerperRateLimitedError,
    SerperResponseError,
    SerperUnavailableError,
)
from .serper_map_type import SearchResult

logger = get_logger(__name__)

KEY_READY = "ready"
KEY_DRY = "dry"
KEY_DEAD = "dead"

#: How long a key waits before retrying a page Serper refused for now.
TRANSIENT_BACKOFF_SECONDS = 0.5
#: How long a rate-limited key waits when Serper gives no Retry-After.
DEFAULT_RATE_LIMIT_WAIT = 2.0


@dataclass(frozen=True)
class SerperMapEvent:
    """One thing that happened during a run, for a caller to print or forward."""

    kind: str
    page: SerperMapPage | None = None
    key_index: int | None = None
    detail: str = ""
    done: int | None = None
    total: int | None = None


@dataclass
class KeySlot:
    """One API key and the state the run gives it."""

    index: int
    client: SerperMapClient
    state: str = KEY_READY

    @property
    def ready(self) -> bool:
        return self.state == KEY_READY

    async def can_serve(self) -> bool:
        """True when the key reports enough credits for one page."""
        try:
            return await self.client.balance() >= 3
        except Exception as error:  # noqa: BLE001 - an unreadable balance is not a dead key
            logger.warning("key %d: the balance could not be read: %s", self.index, describe(error))
            return True


@dataclass(frozen=True)
class SerperMapRunSummary:
    """What a drain did, for the service to turn into a job status."""

    done: int = 0
    failed: int = 0
    pending: int = 0
    dead_keys: int = 0
    dry_keys: int = 0
    keys_used: int = 0

    @property
    def exhausted(self) -> bool:
        """Work is left and no key can take it."""
        return self.pending > 0 and self.dead_keys + self.dry_keys >= self.keys_used


#: Handle one page that answered: save its rows and return how many were saved.
PageHandler = Callable[[SerperMapPage, SearchResult, int], Awaitable[int]]
#: Record one page that ended in a permanent failure.
FailureHandler = Callable[[SerperMapPage, int | None, str], Awaitable[None]]


class SerperMapScheduler:
    """Runs every page through the key pool.

    Each key is its own task. A key fires ``parallel`` pages at a time, waits
    ``rest_seconds`` when there is more work, and then takes the next batch. A
    key that runs dry or is rejected stops for the rest of the run and hands
    its pages back, so the other keys carry on.
    """

    def __init__(
        self,
        pages: list[SerperMapPage],
        clients: list[SerperMapClient],
        *,
        parallel: int = 5,
        rest_seconds: float = 1.5,
        max_rate_limit_retries: int = 3,
        max_transient_retries: int = 2,
        on_event: Callable[[SerperMapEvent], None] | None = None,
        on_failure: FailureHandler | None = None,
        cancelled: asyncio.Event | None = None,
    ):
        self._queue: asyncio.Queue[SerperMapPage] = asyncio.Queue()
        for page in pages:
            self._queue.put_nowait(page)
        self._slots = [KeySlot(index, client) for index, client in enumerate(clients)]
        self._total = len(pages)
        self._parallel = parallel
        self._rest_seconds = rest_seconds
        self._max_rate_limit_retries = max_rate_limit_retries
        self._max_transient_retries = max_transient_retries
        self._on_event = on_event
        self._on_failure = on_failure
        self._cancelled = cancelled or asyncio.Event()
        self._done = 0
        self._failed = 0
        self._lock = asyncio.Lock()

    @property
    def slots(self) -> list[KeySlot]:
        return self._slots

    async def run(self, handle: PageHandler) -> SerperMapRunSummary:
        """Drain every page, then report what is left."""
        if not self._slots:
            raise SerperConfigError("no API key was given, so nothing can be fetched")
        self._emit(SerperMapEvent("started", done=0, total=self._total))
        await asyncio.gather(
            *(self._work(slot, handle) for slot in self._slots),
            return_exceptions=False,
        )
        pending = self._queue.qsize()
        summary = SerperMapRunSummary(
            done=self._done,
            failed=self._failed,
            pending=pending,
            dead_keys=sum(1 for slot in self._slots if slot.state == KEY_DEAD),
            dry_keys=sum(1 for slot in self._slots if slot.state == KEY_DRY),
            keys_used=len(self._slots),
        )
        self._emit(
            SerperMapEvent("finished", done=self._done, total=self._total)
        )
        return summary

    async def _work(self, slot: KeySlot, handle: PageHandler) -> None:
        while slot.ready and not self._cancelled.is_set():
            batch = self._take(self._parallel)
            if not batch:
                return
            await asyncio.gather(*(self._process(slot, page, handle) for page in batch))
            if self._queue.empty() or not slot.ready or self._cancelled.is_set():
                continue
            await self._sleep(self._rest_seconds)

    def _take(self, count: int) -> list[SerperMapPage]:
        batch: list[SerperMapPage] = []
        while len(batch) < count:
            try:
                batch.append(self._queue.get_nowait())
            except asyncio.QueueEmpty:
                break
        return batch

    async def _process(
        self, slot: KeySlot, page: SerperMapPage, handle: PageHandler
    ) -> None:
        """Fetch one page on one key, retrying only what is worth retrying."""
        transient = 0
        limited = 0
        while True:
            if self._cancelled.is_set():
                self._queue.put_nowait(page)
                return
            try:
                result = await slot.client.search(
                    page.keyword, page.lat, page.lng, page.zoom, page.page
                )
            except SerperNoCreditsError as error:
                self._retire(slot, KEY_DRY, "key_dry", page, describe(error))
                return
            except SerperAuthError as error:
                self._retire(slot, KEY_DEAD, "key_dead", page, describe(error))
                return
            except SerperRateLimitedError as error:
                limited += 1
                if limited > self._max_rate_limit_retries:
                    # The concern is the key, not the page: another key may serve it.
                    self._retire(slot, KEY_DEAD, "key_dead", page, describe(error))
                    return
                wait = (
                    error.retry_after
                    if error.retry_after is not None
                    else DEFAULT_RATE_LIMIT_WAIT
                )
                logger.warning(
                    "key %d: rate limited on page %d, waiting %.1fs",
                    slot.index,
                    page.page,
                    wait,
                )
                await self._sleep(wait)
                continue
            except (SerperUnavailableError, SerperResponseError) as error:
                transient += 1
                if transient > self._max_transient_retries:
                    await self._fail(page, None, describe(error))
                    return
                await self._sleep(TRANSIENT_BACKOFF_SECONDS)
                continue

            try:
                await handle(page, result, slot.index)
            except Exception as error:  # noqa: BLE001 - one bad page must not stop the run
                logger.exception("page %d of %r could not be saved", page.page, page.keyword)
                await self._fail(page, None, describe(error))
                return
            async with self._lock:
                self._done += 1
                done = self._done
            self._emit(
                SerperMapEvent(
                    "request_done",
                    page=page,
                    key_index=slot.index,
                    done=done,
                    total=self._total,
                )
            )
            return

    def _retire(
        self,
        slot: KeySlot,
        state: str,
        kind: str,
        page: SerperMapPage,
        detail: str,
    ) -> None:
        """Stop a key for the rest of the run and hand its page to another key."""
        slot.state = state
        self._queue.put_nowait(page)
        logger.warning("key %d retired (%s): %s", slot.index, state, detail)
        self._emit(SerperMapEvent(kind, page=page, key_index=slot.index, detail=detail))

    async def _fail(self, page: SerperMapPage, status: int | None, error: str) -> None:
        async with self._lock:
            self._failed += 1
        if self._on_failure is not None:
            await self._on_failure(page, status, error)
        self._emit(
            SerperMapEvent(
                "request_failed",
                page=page,
                detail=error,
                done=self._done,
                total=self._total,
            )
        )

    def _emit(self, event: SerperMapEvent) -> None:
        if self._on_event is not None:
            self._on_event(event)

    async def _sleep(self, seconds: float) -> None:
        """Wait between batches; a seam so tests need not sleep."""
        if seconds <= 0:
            await asyncio.sleep(0)
            return
        try:
            await asyncio.wait_for(self._cancelled.wait(), timeout=seconds)
        except TimeoutError:
            return
