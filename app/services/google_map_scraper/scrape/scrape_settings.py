"""The base schema of a scraping scrape.

The CLI, the REST API and direct library calls all validate their input against
:class:`ScrapeSettings` or a subclass of it, so the rules are written once.  Field
aliases are the keys stored with each scrape record, so older records still load.
"""

from typing import Annotated, Any, Self

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from app.utils.proxies import parse_proxy_urls
from app.utils.validation import is_clean_text


def _no_control_characters(value: str) -> str:
    if not is_clean_text(value):
        raise ValueError("must not contain control characters")
    return value


CleanText = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=500),
    AfterValidator(_no_control_characters),
]
Language = Annotated[str, StringConstraints(to_lower=True, pattern=r"^[A-Za-z]{2}$")]
Country = Annotated[str, StringConstraints(to_lower=True, pattern=r"^([A-Za-z]{2})?$")]


class ScrapeSettings(BaseModel):
    """Everything one scraping scrape needs to know.

    Field names are the engine's names; the aliases are the keys of the stored
    record and of the REST body.  Latitude and longitude are both set or both
    unset; unset means the search is not anchored to a point.
    """

    model_config = ConfigDict(
        frozen=True,
        extra="ignore",
        validate_by_name=True,
        validate_by_alias=True,
        # Proxy URLs can hold passwords; a validation error must not echo them.
        hide_input_in_errors=True,
    )

    keywords: tuple[CleanText, ...] = Field(
        min_length=1,
        max_length=500,
        description="Search terms; the scrape visits each one in turn.",
    )
    language: Language = Field(
        default="en",
        alias="lang",
        description="Interface language Google is asked for, 2 letters.",
    )
    country: Country = Field(
        default="",
        description="Country code, 2 letters, that the search is limited to.",
    )
    latitude: float | None = Field(default=None, alias="lat", ge=-90, le=90)
    longitude: float | None = Field(default=None, alias="lon", ge=-180, le=180)
    zoom: int = Field(
        default=15, ge=1, le=21, strict=True, description="Map zoom level."
    )
    scroll_depth: int = Field(
        default=1,
        alias="depth",
        ge=1,
        le=1000,
        strict=True,
        description="Batches of results to read per keyword.",
    )
    max_seconds: int = Field(
        default=600,
        alias="max_time",
        ge=1,
        le=86400,
        strict=True,
        description="Time limit of the scrape in seconds.",
    )
    fast_mode: bool = Field(default=False, strict=True)
    collect_emails: bool = Field(
        default=False,
        alias="email",
        strict=True,
        description="Look for emails on each lead's website.",
    )
    collect_socials: bool = Field(
        default=False,
        alias="socials",
        strict=True,
        description="Look for social profiles on each lead's website.",
    )
    proxies: tuple[str, ...] = Field(
        default=(),
        max_length=100,
        description="Proxies the keywords take turns through.",
    )
    limit: int | None = Field(
        default=None,
        ge=1,
        strict=True,
        description="Stop the search once this many leads are collected.",
    )
    crawl_max_pages: int = Field(
        default=100,
        ge=1,
        le=1000,
        strict=True,
        description="Pages the website crawler reads per site.",
    )
    crawl_max_depth: int = Field(
        default=3,
        ge=0,
        le=10,
        strict=True,
        description="Link depth the website crawler follows from the first page.",
    )

    @model_validator(mode="before")
    @classmethod
    def _zero_point_means_none(cls, data: Any) -> Any:
        """Scrapes saved before the point was optional stored 0, 0 for "no point"."""
        if isinstance(data, dict) and data.get("lat") == 0 and data.get("lon") == 0:
            return {**data, "lat": None, "lon": None}
        return data

    @field_validator("proxies")
    @classmethod
    def _proxies_are_valid(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(parse_proxy_urls(list(value)))

    @model_validator(mode="after")
    def _point_is_complete(self) -> Self:
        if (self.latitude is None) != (self.longitude is None):
            raise ValueError("latitude and longitude must be given together")
        return self

    @property
    def locale_tag(self) -> str:
        """Browser locale such as ``de`` or ``de-DE``."""
        return self.language + (f"-{self.country.upper()}" if self.country else "")
