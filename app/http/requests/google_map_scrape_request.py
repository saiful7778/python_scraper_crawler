from pydantic import ConfigDict, Field

from app.services.google_map_scraper.scrape.scrape_settings import (
    CleanText,
    ScrapeSettings,
)


class GoogleMapScrapeRequest(ScrapeSettings):
    """Body of a REST request that creates a Google Maps scrape: the scrape settings plus a name.

    Unknown fields are rejected, so a typo in a field name is an error, not a silent default.
    """

    model_config = ConfigDict(extra="forbid")

    name: CleanText = Field(
        max_length=200, description="Scrape name shown in the scrape list."
    )
