import logging
import time
from dataclasses import dataclass
from typing import cast

from app.utils.errors import ScraperError

from .engine_config import EngineConfig
from .job_settings import JobSettings
from .job_status import JobStatus
from .job_store import JobStore
from .job_types import LeadRow, SettingsData
from .job_worker import JobWorker

logger = logging.getLogger(__name__)

_POLL_SECONDS = 1


class JobDeletedError(ScraperError):
    """The job was deleted while it was running, so it has no result."""


class JobWorkerDiedError(ScraperError):
    """The job worker thread stopped before the job reached an outcome."""


@dataclass(frozen=True)
class JobResult:
    """The outcome of one job run to its end."""

    job_id: str
    status: str
    note: str
    rows: list[LeadRow]
    blocked: bool


def check_job_requirements(settings: JobSettings) -> None:
    """Raise RuntimeError when this machine cannot run the job's settings."""
    if settings.use_browser_for_websites:
        from app.services.browser import check_renderer_available

        check_renderer_available()


def run_job(
    name: str, settings: JobSettings, *, config: EngineConfig | None = None
) -> JobResult:
    """Store the job, run it through the engine and wait until it has an outcome.

    :raises JobDeletedError: If the job is deleted while it runs.
    :raises JobWorkerDiedError: If the worker thread stops before the job
        has a status, so the wait would otherwise never end.
    """
    config = config or EngineConfig()
    check_job_requirements(settings)
    store = JobStore(str(config.data_dir))
    worker = JobWorker(store, headless=config.headless)
    try:
        job_id = store.create_job(
            name,
            cast(SettingsData, settings.model_dump(by_alias=True, mode="json")),
        )
        worker.start()
        while True:
            job = store.get_job(job_id)
            if job is None:
                raise JobDeletedError(f"job {job_id} was deleted while it ran")
            if job["Status"] in (JobStatus.OK, JobStatus.FAILED):
                break
            if not worker.is_alive():
                raise JobWorkerDiedError(
                    f"the job worker stopped while job {job_id} was {job['Status']}"
                )
            time.sleep(_POLL_SECONDS)
        return JobResult(
            job_id=job_id,
            status=job["Status"],
            note=job["Error"],
            rows=store.read_rows(job_id),
            blocked=bool(job["Progress"].get("blocked")),
        )
    finally:
        try:
            worker.shutdown(wait=1)
        except Exception:
            logger.error("stopping the job worker failed", exc_info=True)
        finally:
            store.close()
