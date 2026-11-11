"""The records a crawl passes between the crawler, the Redis buffer and the Postgres copy."""

from dataclasses import dataclass, field
from typing import Any

FINISHED_STATUSES = ("done", "partial", "failed")
WEBSITE_TECH_FIELDS: tuple[str, ...] = (
    "fb_pixel",
    "google_ads_tag",
    "analytics",
    "bing_ads",
    "linkedin_insight",
    "tiktok_pixel",
    "cms",
    "mobile_friendly",
)


@dataclass(frozen=True)
class PageSeed:
    url: str
    normalized_url: str
    depth: int
    discovery_source: str


@dataclass(frozen=True)
class PageResult:
    status: str
    skip_reason: str | None = None
    http_status: int | None = None
    content_type: str | None = None
    content_length: int | None = None
    final_url: str | None = None
    title: str | None = None
    canonical_url: str | None = None
    content_hash: str | None = None
    error_code: str | None = None
    error_message: str | None = None


@dataclass(frozen=True)
class LinkSeed:
    target_normalized_url: str
    anchor_text: str | None
    rel: str | None
    is_internal: bool


@dataclass
class WebsiteSnapshot:
    """Every buffered row of one website, as dicts keyed by the Postgres field names."""

    website: dict[str, Any]
    runs: list[dict[str, Any]] = field(default_factory=list)
    pages: list[dict[str, Any]] = field(default_factory=list)
    links: list[dict[str, Any]] = field(default_factory=list)
    findings: list[dict[str, Any]] = field(default_factory=list)
    events: list[dict[str, Any]] = field(default_factory=list)
