"""Where the scrape engine keeps its data and whether its browser shows a window.

The values come from ``SCRAPER_*`` environment variables, so the CLI, the REST
API and a script all read the same config without passing it around.
"""

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class EngineConfig(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="SCRAPER_", extra="ignore")

    data_dir: Path = Path(".engine/data")
    headless: bool = True
