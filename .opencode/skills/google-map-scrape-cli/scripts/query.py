#!/usr/bin/env python3
"""Run a read-only SQL statement against the scraper's PostgreSQL.

Run from the repository root:

    uv run python .opencode/skills/google-map-scrape-cli/scripts/query.py \
      "SELECT count(*) AS leads FROM leads"

Always add a LIMIT when selecting rows: this prints every row it receives.
"""

import asyncio
import sys
from pathlib import Path

from dotenv import load_dotenv


def _repo_root() -> Path:
    """The nearest ancestor with pyproject.toml, or the current directory."""
    for parent in Path(__file__).resolve().parents:
        if (parent / "pyproject.toml").exists():
            return parent
    return Path.cwd()


# app/config modules read DB_URL at import time, so .env must load first.
load_dotenv(_repo_root() / ".env", override=False)

from tortoise import Tortoise, connections

import bootstrap.application  # noqa: F401 -- logging setup
from config.tortoise import TORTOISE_ORM

READ_ONLY = {"SELECT", "WITH", "SHOW", "EXPLAIN", "VALUES", "TABLE", "DESCRIBE"}


async def main(sql: str) -> None:
    await Tortoise.init(config=TORTOISE_ORM)
    try:
        rows = await connections.get("default").execute_query_dict(sql)
        for row in rows:
            print(row)
        print(f"({len(rows)} row{'s' if len(rows) != 1 else ''})")
    finally:
        await Tortoise.close_connections()


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit('usage: query.py "SELECT ..."')
    statement = " ".join(sys.argv[1:]).strip()
    verb = statement.split(None, 1)[0].upper().rstrip(";")
    if verb not in READ_ONLY:
        sys.exit(
            "only read-only statements are allowed "
            f"({', '.join(sorted(READ_ONLY))}); got {verb!r}"
        )
    asyncio.run(main(statement))
