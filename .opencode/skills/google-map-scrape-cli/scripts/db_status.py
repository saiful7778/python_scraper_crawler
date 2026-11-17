#!/usr/bin/env python3
"""Read-only snapshot of the Google Maps scraper's PostgreSQL state.

Run from the repository root:

    uv run python .opencode/skills/google-map-scrape-cli/scripts/db_status.py

It loads the repository ``.env`` and uses the same ``DB_URL`` as the CLI, so it
reports on the database the scrape actually writes to.
"""

import asyncio
import json
import os
import urllib.parse
from collections import Counter
from pathlib import Path

from dotenv import load_dotenv
from redis import Redis


def _repo_root() -> Path:
    """The nearest ancestor with pyproject.toml, or the current directory."""
    for parent in Path(__file__).resolve().parents:
        if (parent / "pyproject.toml").exists():
            return parent
    return Path.cwd()


# app/config modules read DB_URL at import time, so .env must load first.
load_dotenv(_repo_root() / ".env", override=False)

from tortoise import Tortoise

import bootstrap.application  # noqa: F401 -- logging setup
from app.models import (
    GoogleMapProfileModel,
    LeadEmailModel,
    LeadModel,
    LeadSocialModel,
    ScrapeModel,
)
from config.redis import RedisConfig
from config.tortoise import TORTOISE_ORM


def _text(value) -> str:
    return "" if value is None else str(value)


def _database_name() -> str:
    url = urllib.parse.urlsplit(os.environ.get("DB_URL", ""))
    return f"{url.hostname or 'default'}/{url.path.lstrip('/')}"


def _tile_label(raw: str | None) -> str:
    if not raw:
        return ""
    try:
        tile = json.loads(raw)
    except (TypeError, ValueError):
        return ""
    name = tile.get("name") or tile.get("keyword")
    return f" first={name!r}" if name else ""


def _print_queues() -> None:
    """Show the Redis tile queues; a non-zero queue means the next run resumes it."""
    config = RedisConfig()
    url = urllib.parse.urlsplit(config.url)
    print(
        "redis:",
        f"{url.hostname or 'default'}:{url.port or 6379}/{url.path.lstrip('/') or 0}",
    )
    try:
        client = Redis.from_url(config.url, decode_responses=True)
        client.ping()
    except Exception as error:  # noqa: BLE001 - a status report must not crash
        print("place queues: unreachable:", error)
        return
    try:
        bases = sorted(
            {key.rsplit(":", 1)[0] for key in client.scan_iter("place_queue:*")}
        )
        if not bases:
            print("place queues: none")
            return
        print("place queues:")
        for base in bases:
            pending = client.llen(f"{base}:pending")
            in_flight = client.llen(f"{base}:in_flight")
            done = client.llen(f"{base}:done")
            raw = client.lindex(f"{base}:pending", 0)
            if raw is None:
                raw = next(iter(client.hvals(f"{base}:in_flight_data")), None)
            print(
                f"  - {base.removeprefix('place_queue:')}: "
                f"pending={pending} in_flight={in_flight} done={done}"
                f"{_tile_label(raw)}"
            )
    finally:
        client.close()


async def main() -> None:
    print("database:", _database_name())
    await Tortoise.init(config=TORTOISE_ORM)
    try:
        statuses = Counter(await ScrapeModel.all().values_list("status", flat=True))
        print("scrapes:", dict(sorted(statuses.items())))

        working = (
            await ScrapeModel.filter(status="working")
            .order_by("created_at")
            .values("name", "resumes", "created_at")
        )
        if working:
            print(
                f"working: {len(working)} -- the next CLI run resumes these globally, "
                "not only the new query"
            )
            for row in working:
                print(
                    f"  - {row['name']} (resumes={row['resumes']}, {row['created_at']})"
                )

        print(
            "totals:",
            f"leads={await LeadModel.all().count()}",
            f"profiles={await GoogleMapProfileModel.all().count()}",
            f"emails={await LeadEmailModel.all().count()}",
            f"socials={await LeadSocialModel.all().count()}",
        )
        _print_queues()

        print("latest profiles:")
        rows = (
            GoogleMapProfileModel.all()
            .order_by("-created_at")
            .limit(10)
            .values(
                "lead__title",
                "lead__phone",
                "lead__website",
                "review_rating",
                "review_count",
            )
        )
        async for row in rows:
            rating = _text(row["review_rating"])
            reviews = _text(row["review_count"])
            print(
                f"  - {_text(row['lead__title'])} | {_text(row['lead__phone'])} "
                f"| {_text(row['lead__website'])} | rating={rating} ({reviews} reviews)"
            )
    finally:
        await Tortoise.close_connections()


if __name__ == "__main__":
    asyncio.run(main())
