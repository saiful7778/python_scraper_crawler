import pytest
from tortoise.context import TortoiseContext

#: Tortoise caches insert statements by connection name and table, and the SQLite
#: scrape store also has a "scrapes" table on "default".
CONNECTION = "models_test"


@pytest.fixture
async def db():
    with TortoiseContext() as context:
        await context.init(
            config={
                "connections": {
                    CONNECTION: {
                        "engine": "tortoise.backends.sqlite",
                        "credentials": {"file_path": ":memory:"},
                    }
                },
                "apps": {
                    "models": {
                        "models": ["app.models"],
                        "default_connection": CONNECTION,
                    }
                },
                "use_tz": True,
                "timezone": "UTC",
            }
        )
        await context.generate_schemas(safe=True)
        yield context
        await context.close_connections()
