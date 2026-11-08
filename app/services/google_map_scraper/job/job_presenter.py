"""The public (API/UI) view of a stored job."""

from app.utils.proxies import mask_proxy_credentials

from .job_status import JobStage, JobStatus
from .job_types import JobRecord, PublicJob, PublicProgress


def to_public_job(job: JobRecord) -> PublicJob:
    """Return a copy of ``job`` safe and convenient to show to a client.

    Proxy passwords are masked and the raw progress dict is replaced by a
    stable summary (keywords done/total, leads, percent, stage and, when a
    reviews pass runs, reviews done/total).  Jobs created by 1.x versions may
    hold arbitrary data, so every field is read defensively.
    """
    data, progress = job["Data"], job["Progress"]
    total_keywords = len(data.get("keywords") or [])
    stage = progress.get("stage") or (
        JobStage.QUEUED if job["Status"] == JobStatus.PENDING else ""
    )
    is_ok = job["Status"] == JobStatus.OK
    if is_ok:
        stage = JobStage.DONE
    proxies = data.get("proxies")
    raw_proxies = proxies if isinstance(proxies, list) else []
    masked_proxies = [
        mask_proxy_credentials(p) for p in raw_proxies if isinstance(p, str)
    ]
    summary: PublicProgress = {
        "keywords_done": progress.get("done", 0),
        "keywords_total": total_keywords,
        "leads": progress.get("leads", 0),
        "percent": 100 if is_ok else progress.get("percent", 0),
        "stage": stage,
    }
    reviews_total = progress.get("reviews_total")
    if reviews_total:
        summary["reviews_done"] = progress.get("reviews_done", 0)
        summary["reviews_total"] = reviews_total
    return {
        "ID": job["ID"],
        "Name": job["Name"],
        "Date": job["Date"],
        "Status": job["Status"],
        "Data": {**data, "proxies": masked_proxies},
        "Error": job["Error"],
        "Progress": summary,
    }
