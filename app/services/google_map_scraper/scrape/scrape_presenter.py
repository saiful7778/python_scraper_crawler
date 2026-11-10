"""The public (API/UI) view of a stored scrape."""

from app.utils.proxies import mask_proxy_credentials

from .scrape_status import ScrapeStage, ScrapeStatus
from .scrape_types import PublicProgress, PublicScrape, ScrapeRecord


def to_public_scrape(scrape: ScrapeRecord) -> PublicScrape:
    """Return a copy of ``scrape`` safe and convenient to show to a client.

    Proxy passwords are masked and the raw progress dict is replaced by a
    stable summary (keywords done/total, leads, percent, stage and, when a
    reviews pass runs, reviews done/total).  Scrapes created by 1.x versions may
    hold arbitrary data, so every field is read defensively.
    """
    data, progress = scrape["Data"], scrape["Progress"]
    total_keywords = len(data.get("keywords") or [])
    stage = progress.get("stage") or (
        ScrapeStage.QUEUED if scrape["Status"] == ScrapeStatus.PENDING else ""
    )
    is_ok = scrape["Status"] == ScrapeStatus.OK
    if is_ok:
        stage = ScrapeStage.DONE
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
        "ID": scrape["ID"],
        "Name": scrape["Name"],
        "Date": scrape["Date"],
        "Status": scrape["Status"],
        "Data": {**data, "proxies": masked_proxies},
        "Error": scrape["Error"],
        "Progress": summary,
    }
