import hashlib
import json
from collections.abc import Iterable
from typing import Annotated, Any

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    model_validator,
)

from app.services.google_map_scraper.scrape.scrape_settings import CleanText

from .serper_map_errors import SerperGeocodeError

__all__ = ["SerperGeocodeError", "SerperKeywordPlan", "SerperMapConfig"]

Country = Annotated[str, StringConstraints(to_lower=True, pattern=r"^[A-Za-z]{2}$")]

#: The fields that decide which searches a job runs. Pacing is deliberately out:
#: rerunning with another --parallel still continues the same job.
SEARCH_FIELDS = (
    "keywords",
    "plans",
    "city",
    "state",
    "country",
    "has_phone",
    "has_website",
)


def _as_items(value: Any) -> list[Any]:
    """The items of a list, tuple, set or other non-string iterable; anything else is one item."""
    if isinstance(value, (str, bytes, dict)) or not isinstance(value, Iterable):
        return [value] if value is not None else []
    return list(value)


class SerperKeywordPlan(BaseModel):
    """One keyword and the effort its own search gets."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    keyword: CleanText
    depth: int = Field(
        default=1,
        ge=1,
        le=10,
        strict=True,
        description="Serper pages to read for this keyword, 1 to 10.",
    )
    grid: int = Field(
        default=1,
        ge=1,
        le=10,
        strict=True,
        description="Split the city into N x N tiles for this keyword, 1 to 10.",
    )


class SerperMapConfig(BaseModel):
    """One Serper maps scrape: what to search, where, and how deep."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    plans: tuple[SerperKeywordPlan, ...] = Field(
        default=(),
        max_length=50,
        description="Each keyword with its own depth and grid.",
    )
    keywords: tuple[CleanText, ...] = Field(
        default=(),
        max_length=50,
        description="The keywords of the plans, in the same order.",
    )
    city: CleanText = Field(max_length=200)
    state: CleanText | None = Field(default=None, max_length=200)
    country: Country
    parallel: int = Field(
        default=5,
        ge=1,
        le=20,
        strict=True,
        description="Requests in flight per API key.",
    )
    has_phone: bool = Field(default=False, strict=True)
    has_website: bool = Field(default=False, strict=True)

    @model_validator(mode="before")
    @classmethod
    def _derive_plans(cls, data: Any) -> Any:
        """Accept keywords with one depth and grid, the way a flat config reads."""
        if not isinstance(data, dict):
            return data
        data = dict(data)
        plans = _as_items(data.get("plans"))
        keywords = _as_items(data.get("keywords"))
        # A flat depth/grid only means something for keywords that have no plan:
        # silently dropping one would hide it, so a flat value beside plans is refused.
        depth = data.pop("depth", 1)
        grid = data.pop("grid", 1)
        if plans:
            if depth != 1 or grid != 1:
                raise ValueError(
                    "give the depth and grid inside the plans, not once for every keyword"
                )
            names = [
                plan.get("keyword", "") if isinstance(plan, dict) else plan
                for plan in plans
            ]
            if keywords and list(keywords) != names:
                raise ValueError(
                    "give either the keywords or their plans, not both with different names"
                )
            data["keywords"] = names
            return data
        if keywords:
            data["plans"] = [
                {"keyword": keyword, "depth": depth, "grid": grid}
                for keyword in keywords
            ]
        return data

    @model_validator(mode="after")
    def _at_least_one_keyword(self) -> "SerperMapConfig":
        if not self.plans:
            raise ValueError("give at least one keyword")
        return self

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
    def uses_grid(self) -> bool:
        """True when any keyword splits the city into tiles."""
        return any(plan.grid > 1 for plan in self.plans)

    @property
    def config_key(self) -> str:
        """A hash of the search fields, so a rerun finds its own unfinished job."""
        data = json.dumps(
            self.model_dump(include=set(SEARCH_FIELDS), mode="json"),
            sort_keys=True,
            ensure_ascii=False,
        )
        return hashlib.sha256(data.encode("utf-8")).hexdigest()
