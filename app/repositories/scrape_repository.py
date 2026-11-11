from typing import cast
from uuid import UUID

from tortoise.transactions import in_transaction

from app.models import ScrapeModel
from app.services.google_map_scraper.scrape.scrape_status import ScrapeStatus
from app.services.google_map_scraper.scrape.scrape_types import (
    ProgressData,
    ScrapeRecord,
    SettingsData,
)

from .base import BaseRepository

ERROR_MAX_LENGTH = 500


class ScrapeRepository(BaseRepository):
    def __init__(self):
        self._model = ScrapeModel

    @staticmethod
    def _to_record(scrape: ScrapeModel) -> ScrapeRecord:
        progress = cast(ProgressData, {**scrape.progress, "resumes": scrape.resumes})
        return {
            "ID": str(scrape.id),
            "Name": scrape.name,
            "Date": scrape.created_at.isoformat(timespec="seconds"),
            "Status": scrape.status,
            "Data": scrape.settings,
            "Error": scrape.error,
            "Progress": progress,
        }

    @staticmethod
    def _merge_progress(scrape: ScrapeModel, progress: ProgressData) -> None:
        values = dict(progress)
        resumes = values.pop("resumes", None)
        if resumes is not None:
            scrape.resumes = resumes
        scrape.progress = {**scrape.progress, **values}

    async def create(
        self,
        scrape_id: UUID,
        name: str,
        settings: SettingsData,
        keywords: list[str],
    ) -> None:
        async with in_transaction():
            last = await self._model.all().order_by("-created_order").first()
            await self._model.create(
                id=scrape_id,
                name=name,
                status=ScrapeStatus.PENDING,
                created_order=(last.created_order + 1) if last else 1,
                settings=settings,
                keywords=keywords,
            )

    async def claim_oldest_pending(self) -> ScrapeRecord | None:
        async with in_transaction():
            scrape = (
                await self._model.filter(status=ScrapeStatus.PENDING)
                .order_by("created_order")
                .select_for_update(skip_locked=True)
                .first()
            )
            if scrape is None:
                return None
            scrape.status = ScrapeStatus.WORKING
            await scrape.save(update_fields=["status", "updated_at"])
        return self._to_record(scrape)

    async def list_all(self) -> list[ScrapeRecord]:
        scrapes = await self._model.all().order_by("-created_order")
        return [self._to_record(scrape) for scrape in scrapes]

    async def get(self, scrape_id: UUID | str) -> ScrapeRecord | None:
        scrape = await self._model.get_or_none(id=scrape_id)
        return self._to_record(scrape) if scrape else None

    async def set_status(
        self, scrape_id: UUID | str, status: str, error: str = ""
    ) -> None:
        await self._model.filter(id=scrape_id).update(
            status=status, error=error[:ERROR_MAX_LENGTH]
        )

    async def save_progress(
        self, scrape_id: UUID | str, progress: ProgressData
    ) -> None:
        async with in_transaction():
            scrape = await self._model.get_or_none(id=scrape_id)
            if scrape is None:
                return
            self._merge_progress(scrape, progress)
            await scrape.save(update_fields=["progress", "resumes", "updated_at"])

    async def finish(
        self,
        scrape_id: UUID | str,
        status: str,
        error: str,
        progress: ProgressData,
    ) -> bool:
        async with in_transaction():
            scrape = await self._model.get_or_none(id=scrape_id)
            if scrape is None:
                return False
            self._merge_progress(scrape, progress)
            scrape.status = status
            scrape.error = error[:ERROR_MAX_LENGTH]
            await scrape.save(
                update_fields=["status", "error", "progress", "resumes", "updated_at"]
            )
        return True

    async def requeue_interrupted(self, max_resumes: int) -> tuple[int, int]:
        resumed = failed = 0
        async with in_transaction():
            for scrape in await self._model.filter(status=ScrapeStatus.WORKING):
                if scrape.resumes >= max_resumes:
                    scrape.status = ScrapeStatus.FAILED
                    scrape.error = (
                        f"the engine stopped while this scrape ran ({scrape.resumes + 1} times); "
                        "its rows so far are kept"
                    )
                    await scrape.save(update_fields=["status", "error", "updated_at"])
                    failed += 1
                else:
                    scrape.resumes += 1
                    scrape.status = ScrapeStatus.PENDING
                    await scrape.save(
                        update_fields=["status", "resumes", "updated_at"]
                    )
                    resumed += 1
        return resumed, failed

    async def delete(self, scrape_id: UUID | str) -> bool:
        return bool(await self._model.filter(id=scrape_id).delete())
