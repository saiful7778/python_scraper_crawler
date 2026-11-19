"""A search for one or more keywords in one city: the schema behind every place-level scrape."""

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from .scrape.scrape_settings import CleanText, ScrapeSettings

CountryCode = Annotated[str, StringConstraints(to_lower=True, pattern=r"^[A-Za-z]{2}$")]


class KeywordPlan(BaseModel):
    """One keyword and the effort its own search gets."""

    model_config = ConfigDict(frozen=True)

    keyword: CleanText
    depth: int = Field(default=5, ge=1, le=10, strict=True)
    grid: int = Field(default=1, ge=1, le=10, strict=True)


class PlaceQuery(ScrapeSettings):
    """Keywords and city, plus how to tile the city and which rows to keep."""

    city: CleanText = Field(max_length=200, description="City to search in.")
    state: CleanText | None = Field(
        default=None, max_length=200, description="Optional state or region."
    )
    country: CountryCode = Field(description="Country code, 2 letters (us, de, br).")
    website_depth: Literal[1] = Field(
        default=1,
        description=("1: emails, socials, tech from the website"),
    )
    grid: int | None = Field(
        default=None,
        ge=1,
        le=10,
        strict=True,
        description="Split the city into N x N tiles.",
    )
    keyword_plans: tuple[KeywordPlan, ...] = Field(
        default=(),
        alias="plans",
        max_length=500,
        description="Each keyword with its own depth and grid.",
    )

    @model_validator(mode="before")
    @classmethod
    def _derive_fields(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        data = dict(data)
        plans = data.get("plans") or data.get("keyword_plans")
        keywords = data.get("keywords")
        if plans:
            data["keywords"] = [
                plan["keyword"] if isinstance(plan, dict) else plan.keyword
                for plan in plans
            ]
        elif isinstance(keywords, (list, tuple)) and keywords:
            depth = data.get("depth", data.get("scroll_depth", 1))
            grid = data.get("grid") or 1
            data["plans"] = [
                {"keyword": keyword, "depth": depth, "grid": grid}
                for keyword in keywords
            ]
        data["email"] = True
        data["socials"] = True
        return data

    def depth_of(self, keyword_index: int) -> int:
        """Batches the keyword at this 1-based index searches with."""
        return self.keyword_plans[keyword_index - 1].depth

    def grid_of(self, keyword_index: int) -> int:
        """Grid side of the keyword at this 1-based index; 1 is the whole city."""
        return self.keyword_plans[keyword_index - 1].grid

    @property
    def uses_grid(self) -> bool:
        """True when any keyword splits the city into tiles."""
        return any(plan.grid > 1 for plan in self.keyword_plans)

    def engine_settings(self) -> ScrapeSettings:
        """The settings the scrape engine runs with; the place-only fields are left out.

        The row limit reaches the engine only when nothing trims rows after the
        search (filters, grid bounding box), so the engine never stops too early.
        """
        data = self.model_dump(include=set(ScrapeSettings.model_fields))
        if self.has_phone or self.has_website or self.uses_grid:
            data["limit"] = None
        return ScrapeSettings.model_validate(data)
