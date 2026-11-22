import hashlib
import json
from typing import Annotated

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
)

from app.services.google_map_scraper.scrape.scrape_settings import CleanText

from .serper_map_errors import SerperGeocodeError

__all__ = ["SerperGeocodeError", "SerperMapConfig"]

Country = Annotated[str, StringConstraints(to_lower=True, pattern=r"^[A-Za-z]{2}$")]

#: The fields that decide which searches a job runs. Pacing is deliberately out:
#: rerunning with another --parallel still continues the same job.
SEARCH_FIELDS = (
    "keywords",
    "city",
    "state",
    "country",
    "depth",
    "grid",
    "has_phone",
    "has_website",
)


class SerperMapConfig(BaseModel):
    """One Serper maps scrape: what to search, where, and how deep."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    keywords: tuple[CleanText, ...] = Field(min_length=1, max_length=50)
    city: CleanText = Field(max_length=200)
    state: CleanText | None = Field(default=None, max_length=200)
    country: Country
    depth: int = Field(
        default=1,
        ge=1,
        le=10,
        strict=True,
        description="Serper pages to read per tile, 1 to 10.",
    )
    grid: int = Field(
        default=1,
        ge=1,
        le=10,
        strict=True,
        description="Split the city into N x N tiles, 1 to 10.",
    )
    parallel: int = Field(
        default=5,
        ge=1,
        le=20,
        strict=True,
        description="Requests in flight per API key.",
    )
    has_phone: bool = Field(default=False, strict=True)
    has_website: bool = Field(default=False, strict=True)

    @property
    def place(self) -> str:
        """The place as a reader writes it: ``Austin, TX, US``."""
        parts = (self.city, self.state, self.country.upper())
        return ", ".join(part for part in parts if part)

    @property
    def name(self) -> str:
        """The job's name: its keywords at its place."""
        return f"{', '.join(self.keywords)} @ {self.place}"[:200]

    @property
    def config_key(self) -> str:
        """A hash of the search fields, so a rerun finds its own unfinished job."""
        data = json.dumps(
            self.model_dump(include=set(SEARCH_FIELDS), mode="json"),
            sort_keys=True,
            ensure_ascii=False,
        )
        return hashlib.sha256(data.encode("utf-8")).hexdigest()
