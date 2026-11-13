"""Which leads a scrape keeps, by the phone and website filters."""

from .scrape_settings import ScrapeSettings
from .scrape_types import LeadRow


def _has_value(row: LeadRow, column: str) -> bool:
    return bool(str(row.get(column) or "").strip())


def keep_lead(row: LeadRow, settings: ScrapeSettings) -> bool:
    return (not settings.has_phone or _has_value(row, "phone")) and (
        not settings.has_website or _has_value(row, "website")
    )
