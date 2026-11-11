import asyncio
from collections.abc import Awaitable, Callable
from typing import TypeVar

import fakeredis
import pytest
from tortoise.context import TortoiseContext

from app.services.buffer.flusher import context_config
from config.tortoise import TortoiseConfig

T = TypeVar("T")


@pytest.fixture
def fake_redis():
    return fakeredis.FakeRedis(decode_responses=True)


@pytest.fixture
def postgres(tmp_path):
    """A file-backed SQLite database, so the flusher and the test see the same rows."""
    config = TortoiseConfig(
        connections={
            "default": {
                "engine": "tortoise.backends.sqlite",
                "credentials": {"file_path": str(tmp_path / "crawl.sqlite")},
            }
        },
        apps={
            "models": {
                "models": ["app.models"],
                "default_connection": "default",
                "migrations": "app.migrations",
            }
        },
    )
    asyncio.run(_generate_schemas(config))
    return config


async def _generate_schemas(config: TortoiseConfig) -> None:
    with TortoiseContext() as context:
        await context.init(config=context_config(config))
        await context.generate_schemas(safe=True)
        await context.close_connections()


def in_database(config: TortoiseConfig, action: Callable[[], Awaitable[T]]) -> T:
    """Open the database, run ``action`` on its own loop, and close it again."""

    async def run() -> T:
        with TortoiseContext() as context:
            await context.init(config=context_config(config))
            try:
                return await action()
            finally:
                await context.close_connections()

    return asyncio.run(run())
