import os
from dataclasses import asdict, dataclass, field


@dataclass
class TortoiseConfig:
    connections: dict = field(
        default_factory=lambda: {
            "default": os.getenv("DB_URL", "postgres://user:password@127.0.0.1:5432/db")
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
