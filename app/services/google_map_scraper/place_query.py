"""A search for one keyword in one city: the schema behind every place-level scrape."""

from typing import Annotated, Any, Literal

from pydantic import Field, StringConstraints, model_validator

from .job.job_settings import CleanText, JobSettings

CountryCode = Annotated[str, StringConstraints(to_lower=True, pattern=r"^[A-Za-z]{2}$")]


class PlaceQuery(JobSettings):
    """Keyword and city, plus how to tile the city and which rows to keep."""

    keyword: CleanText = Field(
        description="Business type to search for, e.g. 'coffee shops'."
    )
    city: CleanText = Field(max_length=200, description="City to search in.")
    state: CleanText | None = Field(
        default=None, max_length=200, description="Optional state or region."
    )
    country: CountryCode = Field(description="Country code, 2 letters (us, de, br).")
    website_depth: Literal[1, 2, 3] = Field(
        default=1,
        description=(
            "1: emails and socials from the homepage; 2: also render sites that "
            "need JavaScript; 3: same as 2, the internal crawl is not built yet."
        ),
    )
    grid: int | None = Field(
        default=None,
        ge=1,
        le=10,
        strict=True,
        description="Split the city into N x N tiles.",
    )
    deep_scan: bool = Field(
        default=False,
        strict=True,
        description="Split tiles that reach Google's result cap; needs a grid, 3 by default.",
    )
    has_phone: bool = Field(default=False, strict=True)
    has_email: bool = Field(default=False, strict=True)
    has_website: bool = Field(default=False, strict=True)

    @model_validator(mode="before")
    @classmethod
    def _derive_engine_fields(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        data = dict(data)
        if "keyword" in data and "keywords" not in data:
            data["keywords"] = [data["keyword"]]
        if data.get("deep_scan") is True and data.get("grid") is None:
            data["grid"] = 3
        data["email"] = True
        data["socials"] = True
        data["browser_sites"] = data.get("website_depth", 1) in (2, 3)
        return data

    def engine_settings(self) -> JobSettings:
        """The settings the job engine runs with; the place-only fields are left out.

        The row limit reaches the engine only when nothing trims rows after the
        search (filters, grid bounding box), so the engine never stops too early.
        """
        data = self.model_dump(include=set(JobSettings.model_fields))
        if self.has_phone or self.has_email or self.has_website or self.grid:
            data["limit"] = None
        return JobSettings.model_validate(data)
