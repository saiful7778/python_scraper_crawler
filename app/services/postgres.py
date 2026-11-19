"""Shared PostgreSQL plumbing: connection config, health checks and one long-lived context."""

from dataclasses import asdict
from typing import Any

from asyncpg import InterfaceError  # pyright: ignore[reportMissingTypeStubs]
from asyncpg.exceptions import PostgresConnectionError  # pyright: ignore[reportMissingTypeStubs]
from tortoise.context import TortoiseContext

from app.utils.errors import describe
from app.utils.logger import get_logger
from config.tortoise import TortoiseConfig

logger = get_logger(__name__)

#: Every context the engine opens uses this alias: Tortoise keeps each model's
#: default connection on the model class, so two aliases would override each other.
CONNECTION = "scrape_engine"
#: Errors meaning the PostgreSQL connection itself is gone, not the row is bad.
CONNECTION_LOST = (InterfaceError, ConnectionError, PostgresConnectionError)
#: A cheap query that proves a PostgreSQL connection still answers.
_PING_QUERY = "SELECT 1"


async def check_connection(context: TortoiseContext) -> None:
    """Raise if the context's PostgreSQL connection no longer answers."""
    await context.db().execute_query(_PING_QUERY)


def context_config(
    config: TortoiseConfig, connection: str = CONNECTION
) -> dict[str, Any]:
    settings = asdict(config)
    (credentials,) = settings["connections"].values()
    settings["connections"] = {connection: credentials}
    for app in settings["apps"].values():
        app["default_connection"] = connection
        app.pop("migrations", None)
    return settings


class TortoiseConnection:
    """One long-lived Tortoise context, opened on first use and reopened after a loss."""

    def __init__(self, config: TortoiseConfig):
        self._config = config
        self._context: TortoiseContext | None = None

    @property
    def context(self) -> TortoiseContext | None:
        return self._context

    async def open(self) -> TortoiseContext:
        """The live context, opening it once on first use."""
        if self._context is None:
            context = TortoiseContext()
            context.__enter__()
            try:
                await context.init(config=context_config(self._config))
            except BaseException:
                try:
                    await context.close_connections()
                except Exception as close_error:  # noqa: BLE001 -- the connect error is the one to report
                    logger.warning(
                        "closing the failed PostgreSQL connection also failed: %s",
                        describe(close_error),
                    )
                raise
            finally:
                context.__exit__(None, None, None)
            self._context = context
            logger.info("opened the PostgreSQL connection")
        return self._context

    async def ensure(self) -> TortoiseContext:
        """The live context, opening a new one once if the old connection no longer answers."""
        context = await self.open()
        try:
            await check_connection(context)
        except Exception:  # noqa: BLE001 -- any failure means the connection is unusable
            await self.drop()
            context = await self.open()
            await check_connection(context)
        return context

    async def drop(self) -> None:
        """Forget a lost context and close what is left of it."""
        context, self._context = self._context, None
        if context is None:
            return
        try:
            await context.close_connections()
        except Exception as error:  # noqa: BLE001 -- the connection is already dead
            logger.warning(
                "closing the lost PostgreSQL connection failed: %s", describe(error)
            )

    async def close(self) -> None:
        """Close the live context, if any."""
        context, self._context = self._context, None
        if context is not None:
            await context.close_connections()
