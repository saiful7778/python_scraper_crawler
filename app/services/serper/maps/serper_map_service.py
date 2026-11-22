"""The Serper maps scrape as a service: config in, job out, no process control.

A REST route or a worker calls this the way the CLI does. Nothing here reads the
environment, prints, prompts, or exits.
"""

import asyncio
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import cast
from uuid import UUID

from app.repositories import (
    GoogleMapProfileRepository,
    LeadRepository,
    SerperMapJobInfo,
    SerperMapPage,
    SerperMapRepository,
)
from app.services.google_map_scraper.google_map.geo import (
    BoundingBox,
    is_in_bounding_box,
)
from app.services.google_map_scraper.scrape.dedupe import dedupe_rows, lead_keys
from app.services.google_map_scraper.scrape.lead_crawlability import LeadCrawlability
from app.services.google_map_scraper.scrape.scrape_types import LeadRow
from app.utils.errors import describe
from app.utils.logger import get_logger

from .serper_map_client import CREDITS_PER_PAGE, SerperMapClient
from .serper_map_config import SerperMapConfig
from .serper_map_errors import SerperConfigError, SerperNoCreditsError
from .serper_map_plan import SerperMapPlan, build_plan
from .serper_map_rows import places_to_rows
from .serper_map_state import (
    KEY_DRY,
    SerperMapEvent,
    SerperMapScheduler,
)
from .serper_map_type import SearchResult

logger = get_logger(__name__)

#: How many pages a run may have in flight across all keys at once is decided by
#: the scheduler; this only bounds the saves.
_SAVE_LOCK_TIMEOUT_SECONDS = 120

CrawlCheck = Callable[[list[LeadRow]], None]
EventSink = Callable[[SerperMapEvent], None]
ClientFactory = Callable[[str], SerperMapClient]


@dataclass(frozen=True)
class SerperMapResult:
    """What one run did, for a caller to report or store."""

    job_id: UUID
    status: str
    requests_done: int
    requests_failed: int
    requests_pending: int
    places_found: int
    places_saved: int
    credits_spent: int
    error: str = ""


class SerperMapService:
    """Runs one Serper maps job against one config.

    A service instance runs one job; callers start another instance for another
    job. The caller owns the database lifecycle, the keys, and what happens with
    the events.
    """

    def __init__(
        self,
        config: SerperMapConfig,
        *,
        api_keys: Sequence[str],
        profiles: GoogleMapProfileRepository | None = None,
        known_keys: frozenset[str] | None = None,
        crawlability: CrawlCheck | None = None,
        on_event: EventSink | None = None,
        client_factory: ClientFactory | None = None,
    ):
        self._config = config
        self._api_keys = _clean_keys(api_keys)
        if not self._api_keys:
            raise SerperConfigError("SERPER_API_KEYS is empty: give at least one key")
        self._repository = SerperMapRepository()
        self._profiles = profiles or GoogleMapProfileRepository(LeadRepository())
        self._known: set[str] | None = set(known_keys) if known_keys is not None else None
        self._crawlability = crawlability or LeadCrawlability()
        self._on_event = on_event
        self._client_factory = client_factory or (lambda key: SerperMapClient(key))
        self._clients: list[SerperMapClient] = []
        self._cancelled = asyncio.Event()
        self._job_id: UUID | None = None

    @property
    def config(self) -> SerperMapConfig:
        return self._config

    async def plan(self) -> SerperMapPlan:
        """Work out every page the config asks for, without writing anything."""
        return await build_plan(self._config)

    async def find_unfinished(self) -> SerperMapJobInfo | None:
        """The newest unfinished job of this exact config, if there is one."""
        return await self._repository.find_unfinished(self._config.config_key)

    async def start(self, plan: SerperMapPlan | None = None) -> UUID:
        """Create the job and its request rows, and return the job id."""
        plan = plan or await self.plan()
        stored = self._config.model_dump(mode="json")
        stored["bounding_box"] = list(plan.bounding_box) if plan.bounding_box else None
        info = await self._repository.create_job(
            name=self._config.name,
            config=stored,
            config_key=self._config.config_key,
            pages=plan.pages,
            key_count=len(self._api_keys),
        )
        logger.info(
            "started serper job %s: %d page(s), %d key(s)",
            info.id,
            info.total_requests,
            len(self._api_keys),
        )
        return info.id

    async def resume(self, job_id: UUID) -> UUID:
        """Take an unfinished job back to running and return its id."""
        await self._repository.set_running(job_id)
        return job_id

    async def run(self, job_id: UUID) -> SerperMapResult:
        """Drain every page the job has left, saving rows as pages arrive."""
        self._job_id = job_id
        self._cancelled.clear()
        info = await self._repository.get(job_id)
        if info is None:
            raise SerperConfigError(f"job {job_id} does not exist")
        bounding_box = _bounding_box_of(info.config)
        await self._repository.set_running(job_id)

        pages = await self._repository.pending_pages(job_id)
        if not pages:
            await self._repository.finish(job_id, "finished")
            return self._result(job_id, "finished", "")

        known = await self._known_keys()
        clients, skipped = await self._live_clients()
        if len(clients) == 0:
            status = "failed" if skipped else "exhausted"
            error = (
                "every API key was refused"
                if skipped
                else "every API key is out of credits"
            )
            await self._repository.finish(job_id, status, error)
            return self._result(job_id, status, error)

        if known is None:
            known = set()
        save_lock = asyncio.Lock()
        totals = {"saved": 0}

        async def handle(page: SerperMapPage, result: SearchResult, key_index: int) -> int:
            return await self._handle_page(
                job_id, page, result, key_index, known, save_lock, totals, bounding_box
            )

        async def on_failure(page: SerperMapPage, status: int | None, error: str) -> None:
            await self._repository.mark_failed(
                job_id, page, http_status=status, error=error
            )

        scheduler = SerperMapScheduler(
            pages,
            clients,
            parallel=self._config.parallel,
            on_event=self._on_event,
            on_failure=on_failure,
            cancelled=self._cancelled,
        )
        try:
            summary = await scheduler.run(handle)
        except asyncio.CancelledError:
            await self._repository.finish(job_id, "interrupted", "stopped")
            logger.warning("serper job %s was cancelled", job_id)
            raise
        finally:
            await self._close_clients()

        status, error = self._status_of(summary)
        if self._cancelled.is_set():
            status, error = "interrupted", "stopped"
        await self._repository.finish(job_id, status, error)
        result = await self._result(job_id, status, error)
        logger.info(
            "serper job %s ended as %s: %d done, %d failed, %d pending, %d saved",
            job_id,
            result.status,
            result.requests_done,
            result.requests_failed,
            result.requests_pending,
            result.places_saved,
        )
        return result

    async def cancel(self) -> None:
        """Stop the run between pages; progress already saved stays saved."""
        self._cancelled.set()
        if self._job_id is not None:
            await self._repository.finish(self._job_id, "interrupted", "stopped")

    async def aclose(self) -> None:
        await self._close_clients()

    async def _handle_page(
        self,
        job_id: UUID,
        page: SerperMapPage,
        result: SearchResult,
        key_index: int,
        known: set[str],
        save_lock: asyncio.Lock,
        totals: dict[str, int],
        bounding_box: BoundingBox | None,
    ) -> int:
        rows = places_to_rows(result.places, page.keyword, self._config)
        if bounding_box is not None:
            rows = [row for row in rows if is_in_bounding_box(row, bounding_box)]
        async with save_lock:
            kept = dedupe_rows(rows, frozenset(known))
            if kept and self._config.has_website:
                await asyncio.wait_for(
                    asyncio.to_thread(self._crawlability, kept),
                    timeout=_SAVE_LOCK_TIMEOUT_SECONDS,
                )
            saved = (
                await self._profiles.save_rows(kept, scrape_id=str(job_id))
                if kept
                else 0
            )
            for row in kept:
                known.update(lead_keys(row))
            totals["saved"] += saved
        await self._repository.mark_done(
            job_id,
            page,
            http_status=200,
            places=len(result.places),
            credits=result.credits or CREDITS_PER_PAGE,
            key_hint=key_index,
            saved=saved,
        )
        return saved

    async def _known_keys(self) -> set[str] | None:
        if self._known is not None:
            return self._known
        rows = await self._profiles.key_rows()
        self._known = set(
            key for row in rows for key in _keys_of(row)
        )
        logger.debug("loaded %d known lead keys", len(self._known))
        return self._known

    async def _live_clients(self) -> tuple[list[SerperMapClient], int]:
        """One client per key that still has credits; also count the refused keys."""
        clients: list[SerperMapClient] = []
        skipped = 0
        for key in self._api_keys:
            client = self._client_factory(key)
            try:
                balance = await client.balance()
            except Exception as error:  # noqa: BLE001 - an unreadable balance is not a dead key
                logger.warning("the balance could not be read, the key is used: %s", describe(error))
                balance = CREDITS_PER_PAGE
            if balance < CREDITS_PER_PAGE:
                skipped += 1
                await client.aclose()
                self._emit_dry(len(clients) + skipped - 1)
                continue
            clients.append(client)
        self._clients = clients
        return clients, skipped

    def _emit_dry(self, index: int) -> None:
        if self._on_event is not None:
            self._on_event(SerperMapEvent("key_dry", key_index=index, detail="no credits"))

    async def _close_clients(self) -> None:
        clients, self._clients = self._clients, []
        for client in clients:
            await client.aclose()

    def _status_of(self, summary) -> tuple[str, str]:
        if summary.exhausted:
            return "exhausted", "every API key is out of credits or retired"
        if summary.done == 0 and summary.failed == 0 and summary.pending > 0:
            return "failed", "no page could be fetched"
        if summary.pending > 0:
            return "exhausted", "the run stopped with work left"
        if summary.failed > 0 and summary.done == 0:
            return "failed", "every page failed"
        return "finished", ""

    async def _result(self, job_id: UUID, status: str, error: str) -> SerperMapResult:
        info = await self._repository.get(job_id)
        if info is None:
            return SerperMapResult(
                job_id=job_id,
                status=status,
                requests_done=0,
                requests_failed=0,
                requests_pending=0,
                places_found=0,
                places_saved=0,
                credits_spent=0,
                error=error,
            )
        return SerperMapResult(
            job_id=job_id,
            status=status,
            requests_done=info.done_requests,
            requests_failed=info.failed_requests,
            requests_pending=info.pending_requests,
            places_found=info.places_found,
            places_saved=info.places_saved,
            credits_spent=info.credits_spent,
            error=error,
        )


def _clean_keys(api_keys: Sequence[str]) -> list[str]:
    """Drop blanks and repeats, keeping the order the list gives."""
    seen: dict[str, None] = {}
    for key in api_keys:
        text = str(key).strip()
        if text:
            seen.setdefault(text, None)
    return list(seen)


def _bounding_box_of(config: dict) -> BoundingBox | None:
    box = config.get("bounding_box")
    if not isinstance(box, list) or len(box) != 4:
        return None
    try:
        south, north, west, east = (float(value) for value in box)
    except (TypeError, ValueError):
        return None
    return (south, north, west, east)


def _keys_of(row: dict) -> list[str]:
    """The dedupe keys of one stored row, the way the browser engine reads them."""
    from app.repositories.google_map_profile_repository import KEY_FIELDS

    return [f"{field}:{row[field]}" for field in KEY_FIELDS if row.get(field)]


__all__ = ["SerperMapResult", "SerperMapService", "KEY_DRY"]
