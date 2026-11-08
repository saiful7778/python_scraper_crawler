import asyncio

from app.repositories import GoogleMapProfileRepository
from app.services.google_map_scraper.job.job_types import LeadRow

FINISHED = ("ok", "failed")


class Ingester:
    def __init__(
        self,
        google_map_profile_repository: GoogleMapProfileRepository,
    ):
        self.google_map_profile_repository = google_map_profile_repository

    async def run(self, rows: list[LeadRow]) -> tuple[int, int]:
        saved = failed = 0
        for row in rows:
            try:
                await self.google_map_profile_repository.save_row(row)
                saved += 1
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 -- one bad row must not stop the rest
                failed += 1
                print(
                    f"ingest failed for {row.get('title', '')!r}: {type(e).__name__}: {e}"
                )
        return saved, failed
