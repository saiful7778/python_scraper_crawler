from dataclasses import dataclass

from fastapi_startkit.configuration import Config
from fastapi_startkit.environment import env


@dataclass
class RedisConfig(Config):
    url: str = env("REDIS_URL", "redis://127.0.0.1:6379/0")

    @classmethod
    def from_environment(cls) -> "RedisConfig":
        return cls()
