import asyncio
import contextlib

import fakeredis
import pytest
from tortoise.context import TortoiseContext

from config.tortoise import TortoiseConfig


def sqlite_config(path) -> TortoiseConfig:
    return TortoiseConfig(
        connections={
            "default": {
                "engine": "tortoise.backends.sqlite",
                "credentials": {"file_path": str(path)},
            }
        }
    )


def in_database(config, work=None, generate_schemas=False):
    async def run():
        with TortoiseContext() as context:
            await context.init(
                config={
                    "connections": {"scrape_check": config.connections["default"]},
                    "apps": {
                        "models": {
                            "models": ["app.models"],
                            "default_connection": "scrape_check",
                        }
                    },
                }
            )
            try:
                if generate_schemas:
                    await context.generate_schemas(safe=True)
                return await work() if work else None
            finally:
                await context.close_connections()

    return asyncio.run(run())


@pytest.fixture
def postgres(tmp_path):
    """The configuration of an empty database with every table."""
    config = sqlite_config(tmp_path / "engine.db")
    in_database(config, generate_schemas=True)
    return config


@pytest.fixture
def fake_redis():
    return fakeredis.FakeRedis(decode_responses=True)


class FakeProxyTab:
    tab = object()
    refused = False


class FakeBrowserSession:
    def __init__(self, locale: str, headless: bool):
        self.browser = None

    @property
    def is_dead(self) -> bool:
        return self.browser is None

    def start(self) -> None:
        self.browser = object()

    def restart(self) -> None:
        self.start()

    def kill(self) -> None:
        self.browser = None

    def close(self) -> None:
        self.browser = None

    @contextlib.contextmanager
    def open_tab(self, proxy):
        yield FakeProxyTab()


def fake_collect(*leads: dict):
    """A Maps search that returns fresh copies of ``leads`` for every keyword."""

    def collect(tab, url, **kwargs):
        return [dict(lead) for lead in leads], "done", {"batches": 1, "dom_links": 0}

    return collect


@pytest.fixture
def fake_maps(monkeypatch):
    """A browser and a Maps search that find one bakery; tests may replace the search."""
    monkeypatch.setattr(
        "app.services.google_map_scraper.scrape.scrape_run.BrowserSession",
        FakeBrowserSession,
    )
    monkeypatch.setattr(
        "app.services.google_map_scraper.scrape.keyword_search.collect_leads",
        fake_collect(
            {
                "title": "Bakery Test",
                "website": "https://bakery-test.de",
                "latitude": 52.5,
                "longitude": 13.4,
            }
        ),
    )
    monkeypatch.setattr(
        "app.services.google_map_scraper.scrape.scrape_run.resolve_website",
        lambda raw: raw,
    )
