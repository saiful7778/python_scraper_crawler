from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi_startkit.support import Provider
from tortoise import Tortoise

from config.tortoise import TortoiseConfig


class TortoiseProvider(Provider):
    def register(self) -> None:
        self.tortoise_config = self.resolve_config(TortoiseConfig)

    def boot(self) -> None:
        self.app.fastapi.router.lifespan_context = self.lifespan

    @asynccontextmanager
    async def lifespan(self, app: FastAPI):
        await Tortoise.init(config=self.tortoise_config, _enable_global_fallback=True)
        try:
            yield
        finally:
            await Tortoise.close_connections()
