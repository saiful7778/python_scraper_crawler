#!/usr/bin/env python3
"""Read-only snapshot of the Serper Maps scrape state.

Run from the repository root:

    uv run python .opencode/skills/serper-map-scrape-cli/scripts/serper_status.py
    uv run python .opencode/skills/serper-map-scrape-cli/scripts/serper_status.py --job <uuid>

It loads the repository ``.env`` and uses the same ``DB_URL`` as the CLI, so it
reports on the database the scrape actually writes to. It never prints an API key:
the database stores only a key's 0-based index (``key_hint``).
"""

import argparse
import asyncio
import os
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

#: The statuses whose job still has pages a rerun would fetch.
UNFINISHED = ("running", "exhausted", "interrupted")


async def _jobs(limit: int) -> list[dict]:
    return await connections.get("default").execute_query_dict(
        """
        SELECT id::text AS id, name, status, total_requests, done_requests,
               failed_requests, places_found, places_saved, credits_spent,
               key_count, left(error, 160) AS error, created_at, updated_at
        FROM serper_map_jobs
        ORDER BY created_at DESC
        LIMIT $1
        """,
        [limit],
    )


async def _requests(job_id: str) -> list[dict]:
    return await connections.get("default").execute_query_dict(
        """
        SELECT status, count(*) AS pages, sum(places) AS places,
               sum(credits) AS credits
        FROM serper_map_requests
        WHERE job_id = $1::uuid
        GROUP BY status
        ORDER BY status
        """,
        [job_id],
    )


async def _failed_pages(job_id: str, limit: int) -> list[dict]:
    return await connections.get("default").execute_query_dict(
        """
        SELECT keyword, page, grid_x, grid_y, attempts, http_status,
               left(error, 160) AS error
        FROM serper_map_requests
        WHERE job_id = $1::uuid AND status = 'failed'
        ORDER BY keyword, grid_y, grid_x, page
        LIMIT $2
        """,
        [job_id, limit],
    )


async def _pending_keys(job_id: str) -> list[dict]:
    """Which key served which page; a high count on one key is worth a look."""
    return await connections.get("default").execute_query_dict(
        """
        SELECT key_hint, count(*) AS pages
        FROM serper_map_requests
        WHERE job_id = $1::uuid AND status = 'done' AND key_hint IS NOT NULL
        GROUP BY key_hint
        ORDER BY key_hint
        """,
        [job_id],
    )


def _row(label: str, value: object) -> str:
    return f"  {label:<16} {value}"


async def main(job_id: str | None, limit: int) -> None:
    await Tortoise.init(config=TORTOISE_ORM)
    try:
        print(f"database: {_target()}")
        missing = await _missing_tables()
        if missing:
            print(
                "\n✗ The Serper tables do not exist yet: "
                f"{', '.join(missing)}.\n"
                "  Apply them:\n"
                "    uv run --env-file .env tortoise -c config.tortoise.TORTOISE_ORM migrate"
            )
            return

        jobs = await _jobs(limit)
        if not jobs:
            print("\nNo Serper jobs yet.")
            return

        unfinished = [job for job in jobs if job["status"] in UNFINISHED]
        print(
            f"\n{len(jobs)} recent job(s), {len(unfinished)} unfinished"
            + (" (a rerun of the same flags offers to continue them)" if unfinished else "")
        )
        for job in jobs:
            _print_job(job)
            counts = await _requests(job["id"])
            for count in counts:
                print(
                    _row(
                        f"  {count['status']}",
                        f"{count['pages']} page(s), "
                        f"{count['places'] or 0} places, {count['credits'] or 0} credits",
                    )
                )
            keys = await _pending_keys(job["id"])
            if keys:
                print(
                    _row(
                        "by key",
                        ", ".join(f"#{row['key_hint']}: {row['pages']}" for row in keys),
                    )
                )
            failed = await _failed_pages(job["id"], 5)
            for page in failed:
                print(
                    _row(
                        "  failed page",
                        f"{page['keyword']} page {page['page']} "
                        f"tile ({page['grid_x']},{page['grid_y']}), "
                        f"{page['attempts']} attempt(s), HTTP {page['http_status']}: "
                        f"{page['error']}",
                    )
                )

        if job_id:
            matched = [job for job in jobs if job["id"] == job_id]
            if not matched:
                print(f"\n! Job {job_id} is not among the {len(jobs)} most recent jobs.")
            else:
                print(f"\nJob {job_id} is {'unfinished' if job_id in {j['id'] for j in unfinished} else 'closed'}.")
    finally:
        await Tortoise.close_connections()


def _target() -> str:
    """The database host and name, never the credentials."""
    url = os.getenv("DB_URL", "postgres://user:password@127.0.0.1:5432/db")
    if "@" not in url:
        # No userinfo to hide, but a bare DSN may still be a secret; show only the tail.
        parts = url.split("/")
        return f"/{parts[-1]}" if len(parts) > 1 else "(configured)"
    return url.split("@", 1)[-1]


async def _missing_tables() -> list[str]:
    rows = await connections.get("default").execute_query_dict(
        "SELECT tablename FROM pg_tables "
        "WHERE schemaname = 'public' AND tablename IN "
        "('serper_map_jobs', 'serper_map_requests')"
    )
    found = {row["tablename"] for row in rows}
    return [
        table
        for table in ("serper_map_jobs", "serper_map_requests")
        if table not in found
    ]


def _print_job(job: dict) -> None:
    name = str(job["name"])
    requests = (
        f"{job['done_requests']} done, {job['failed_requests']} failed, "
        f"{job['total_requests']} total"
    )
    places = f"{job['places_saved']} saved of {job['places_found']} found"
    print(
        f"\n{name[:90]}{'…' if len(name) > 90 else ''}\n"
        f"{_row('id', job['id'])}\n"
        f"{_row('status', job['status'])}\n"
        f"{_row('requests', requests)}\n"
        f"{_row('places', places)}\n"
        f"{_row('credits', job['credits_spent'])}\n"
        f"{_row('keys', job['key_count'])}\n"
        f"{_row('started', job['created_at'])}\n"
        f"{_row('last run', job['updated_at'])}"
    )
    if job["error"]:
        print(_row("error", job["error"]))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Show the Serper Maps jobs stored in PostgreSQL."
    )
    parser.add_argument(
        "--job",
        metavar="UUID",
        help=(
            "report whether this job is among the recent ones shown "
            "(it is matched within --limit, not searched for)"
        ),
    )
    parser.add_argument(
        "--limit", type=int, default=10, metavar="N", help="jobs to show (default: 10)"
    )
    args = parser.parse_args()
    try:
        asyncio.run(main(args.job, args.limit))
    except Exception as error:  # noqa: BLE001 - a status tool should not traceback
        sys.exit(f"✗ could not read the Serper job tables: {error}")
