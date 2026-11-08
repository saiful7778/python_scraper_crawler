import os
from dataclasses import asdict, dataclass, field

from app.utils.errors import ConfigError


def _database_port() -> int:
    raw = os.getenv("DB_PORT", "5432")
    try:
        return int(raw)
    except ValueError as error:
        raise ConfigError(f"DB_PORT must be a whole number, not {raw!r}") from error


@dataclass
class TortoiseConfig:
    connections: dict = field(
        default_factory=lambda: {
            "default": {
                "engine": "tortoise.backends.asyncpg",
                "credentials": {
                    "host": os.getenv("DB_HOST", "127.0.0.1"),
                    "port": _database_port(),
                    "user": os.getenv("DB_USERNAME", "user"),
                    "password": os.getenv("DB_PASSWORD", "password"),
                    "database": os.getenv("DB_DATABASE", "db"),
                },
            }
        }
    )

    apps: dict = field(
        default_factory=lambda: {
            "models": {
                "models": ["app.models"],
                "default_connection": "default",
                "migrations": "app.migrations",
            }
        }
    )

    use_tz: bool = True
    timezone: str = "UTC"


TORTOISE_ORM = asdict(TortoiseConfig())
