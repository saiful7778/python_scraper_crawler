from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from tortoise.expressions import F

from app.models import (
    SerperMapJobModel,
    SerperMapJobStatus,
    SerperMapRequestModel,
    SerperMapRequestStatus,
)

from .base import BaseRepository

#: The statuses of a job that still has work a rerun could finish.
UNFINISHED = (
    SerperMapJobStatus.RUNNING,
    SerperMapJobStatus.EXHAUSTED,
    SerperMapJobStatus.INTERRUPTED,
)


@dataclass(frozen=True)
class SerperMapPage:
    """One Serper /maps call: a keyword searched at a tile for one page."""

    keyword: str
    page: int
    grid_x: int
    grid_y: int
    lat: float
    lng: float
    zoom: int

    @property
    def ll(self) -> str:
        """The point, in the ``@lat,lng,zoomz`` form Serper expects."""
        return f"@{self.lat},{self.lng},{self.zoom}z"


@dataclass(frozen=True)
class SerperMapJobInfo:
    """A job as the service and the CLI see it."""

    id: UUID
    name: str
    status: str
    error: str
    config: dict
    total_requests: int
    done_requests: int
    failed_requests: int
    pending_requests: int
    places_found: int
    places_saved: int
    credits_spent: int
    created_at: datetime
    updated_at: datetime


def _page_of(request: SerperMapRequestModel) -> SerperMapPage:
    return SerperMapPage(
        keyword=request.keyword,
        page=request.page,
        grid_x=request.grid_x,
        grid_y=request.grid_y,
        lat=request.lat,
        lng=request.lng,
        zoom=request.zoom,
    )


class SerperMapRepository(BaseRepository):
    """Reads and writes ``serper_map_jobs`` and ``serper_map_requests``."""

    def __init__(self):
        super().__init__()
        self._model = SerperMapJobModel

    @staticmethod
    def _to_info(job: SerperMapJobModel) -> SerperMapJobInfo:
        return SerperMapJobInfo(
            id=job.id,
            name=job.name,
            status=job.status,
            error=job.error,
            config=job.config,
            total_requests=job.total_requests,
            done_requests=job.done_requests,
            failed_requests=job.failed_requests,
            pending_requests=job.total_requests - job.done_requests - job.failed_requests,
            places_found=job.places_found,
            places_saved=job.places_saved,
            credits_spent=job.credits_spent,
            created_at=job.created_at,
            updated_at=job.updated_at,
        )

    async def create_job(
        self,
        name: str,
        config: dict,
        config_key: str,
        pages: list[SerperMapPage],
        key_count: int,
    ) -> SerperMapJobInfo:
        """Insert a running job and one pending request row per page."""
        job = await self._model.create(
            name=name,
            status=SerperMapJobStatus.RUNNING,
            config=config,
            config_key=config_key,
            key_count=key_count,
            total_requests=len(pages),
        )
        if pages:
            await SerperMapRequestModel.bulk_create(
                [
                    SerperMapRequestModel(
                        job_id=job.id,
                        keyword=page.keyword,
                        page=page.page,
                        grid_x=page.grid_x,
                        grid_y=page.grid_y,
                        lat=page.lat,
                        lng=page.lng,
                        zoom=page.zoom,
                        status=SerperMapRequestStatus.PENDING,
                    )
                    for page in pages
                ],
                batch_size=500,
            )
        return self._to_info(job)

    async def find_unfinished(self, config_key: str) -> SerperMapJobInfo | None:
        """The newest job with this config that still has work to finish."""
        job = (
            await self._model.filter(config_key=config_key, status__in=UNFINISHED)
            .order_by("-created_at")
            .first()
        )
        return self._to_info(job) if job else None

    async def get(self, job_id: UUID) -> SerperMapJobInfo | None:
        job = await self._model.get_or_none(id=job_id)
        return self._to_info(job) if job else None

    async def pending_pages(self, job_id: UUID) -> list[SerperMapPage]:
        """Every page a run would still fetch: the pending ones and the failed ones."""
        requests = await SerperMapRequestModel.filter(
            job_id=job_id,
            status__in=(SerperMapRequestStatus.PENDING, SerperMapRequestStatus.FAILED),
        ).order_by("keyword", "grid_y", "grid_x", "page")
        return [_page_of(request) for request in requests]

    async def mark_done(
        self,
        job_id: UUID,
        page: SerperMapPage,
        *,
        http_status: int,
        places: int,
        credits: int,
        key_hint: int,
        saved: int,
    ) -> None:
        """Record a page that answered, and add its numbers to the job's counters."""
        await self._request(job_id, page).update(
            status=SerperMapRequestStatus.DONE,
            http_status=http_status,
            places=places,
            credits=credits,
            key_hint=key_hint,
            error="",
            attempts=F("attempts") + 1,
        )
        await self._model.filter(id=job_id).update(
            done_requests=F("done_requests") + 1,
            places_found=F("places_found") + places,
            places_saved=F("places_saved") + saved,
            credits_spent=F("credits_spent") + credits,
        )

    async def mark_failed(
        self,
        job_id: UUID,
        page: SerperMapPage,
        *,
        http_status: int | None,
        error: str,
    ) -> None:
        """Record a page that ended in a permanent failure.

        A page already counted as failed stays counted once, however often the
        run retries it.
        """
        was_failed = (
            await self._request(job_id, page).first().values_list("status", flat=True)
        ) == SerperMapRequestStatus.FAILED
        await self._request(job_id, page).update(
            status=SerperMapRequestStatus.FAILED,
            http_status=http_status,
            error=error,
            attempts=F("attempts") + 1,
        )
        if not was_failed:
            await self._model.filter(id=job_id).update(
                failed_requests=F("failed_requests") + 1
            )

    async def mark_attempt(self, job_id: UUID, page: SerperMapPage, key_hint: int) -> None:
        await self._request(job_id, page).update(
            attempts=F("attempts") + 1, key_hint=key_hint
        )

    async def set_running(self, job_id: UUID) -> None:
        await self._model.filter(id=job_id).update(
            status=SerperMapJobStatus.RUNNING, error=""
        )

    async def finish(self, job_id: UUID, status: str, error: str = "") -> None:
        await self._model.filter(id=job_id).update(status=status, error=error[:2000])

    def _request(self, job_id: UUID, page: SerperMapPage):
        """The query for one request row of a job."""
        return SerperMapRequestModel.filter(
            job_id=job_id,
            keyword=page.keyword,
            page=page.page,
            grid_x=page.grid_x,
            grid_y=page.grid_y,
        )
