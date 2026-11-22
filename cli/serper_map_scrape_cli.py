"""Scrape Google Maps through Serper: one command, every API key, resumable.

The CLI only parses flags, reads the keys from the environment, prompts, and
prints. The work itself is `app.services.serper.maps.SerperMapService`.
"""

import argparse
import asyncio
import os
import sys
from collections.abc import Awaitable, Callable
from typing import cast

from pydantic import ValidationError
from tortoise import Tortoise

import bootstrap.application  # noqa: F401 -- installs the central logging (config/logging.py)
from app.repositories.serper_map_repository import SerperMapJobInfo
from app.services.serper.maps import (
    SerperError,
    SerperMapConfig,
    SerperMapEvent,
    SerperMapPlan,
    SerperMapResult,
    SerperMapService,
    build_plan,
)
from app.utils.errors import describe
from app.utils.logger import get_logger
from config.tortoise import TORTOISE_ORM

logger = get_logger(__name__)

KEY_VARIABLE = "SERPER_API_KEYS"

FLAG_NAMES = {
    "plans": "--keyword",
    "keywords": "--keyword",
    "has_phone": "--has-phone",
    "has_website": "--has-website",
}


class KeywordAction(argparse.Action):
    """Start a keyword plan: the --depth/--grid that follow belong to it."""

    def __call__(self, parser, namespace, values, option_string=None):
        plan = {"keyword": str(values), "depth": None, "grid": None}
        plans = getattr(namespace, "plans", None)
        if plans is None:
            plans = namespace.plans = []
        plans.append(plan)
        namespace.keyword_scope = plan


class KeywordScopedAction(argparse.Action):
    """Set a value on the plan the last --keyword flag started."""

    def __call__(self, parser, namespace, values, option_string=None):
        plan = getattr(namespace, "keyword_scope", None)
        if plan is None:
            namespace.keyword_plan_error = (
                f"{option_string} must follow a --keyword flag"
            )
            return
        plan[self.dest] = cast(int, values)


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        description="Scrape Google Maps business listings through the Serper API."
    )
    ap.add_argument(
        "--keyword",
        "-k",
        action=KeywordAction,
        metavar="KEYWORD",
        help='a business type to search for, e.g. "plumbers"; repeat it for more keywords, and give each its own --depth/--grid after it',
    )
    ap.add_argument("--city", help='city name, e.g. "Austin"')
    ap.add_argument("--state", help='optional state or region, e.g. "TX"')
    ap.add_argument(
        "--country",
        metavar="XX",
        help="country to search in, 2 letters (e.g. us, de, br)",
    )
    ap.add_argument(
        "--depth",
        type=int,
        action=KeywordScopedAction,
        metavar="N",
        help="Serper pages for that keyword, 1 to 10 (default: 1). Each page is one API call and 3 credits; follows a --keyword flag",
    )
    ap.add_argument(
        "--grid",
        type=int,
        action=KeywordScopedAction,
        metavar="N",
        help="split the city into N x N tiles for that keyword, 1 to 10 (default: 1, the whole city); follows a --keyword flag",
    )
    ap.add_argument(
        "--parallel",
        type=int,
        default=5,
        metavar="N",
        help="requests in flight per API key, 1 to 20 (default: 5). Every key gets its own N.",
    )
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="show the plan and the cost, without scraping or writing",
    )
    ap.add_argument(
        "--no-resume",
        action="store_true",
        help="always start a new job instead of offering to continue an unfinished one",
    )

    f = ap.add_argument_group("filters", "keep only the rows that have the field")
    f.add_argument(
        "--has-phone", action="store_true", help="keep only rows with a phone number"
    )
    f.add_argument(
        "--has-website",
        action="store_true",
        help="keep only rows with a website, and check whether it can be crawled",
    )
    return ap


def read_config(argv: list[str] | None = None) -> SerperMapConfig:
    """Parse the command line into a validated config; exits with the reason if it is invalid."""
    return config_of(build_parser().parse_args(argv))


def config_of(args: argparse.Namespace) -> SerperMapConfig:
    """Validate parsed arguments into a config; exits with one line per problem."""
    values = vars(args).copy()
    values.pop("keyword_scope", None)
    values.pop("keyword", None)
    values.pop("depth", None)
    values.pop("grid", None)
    values.pop("dry_run")
    values.pop("no_resume")
    error = values.pop("keyword_plan_error", None)
    if error:
        logger.warning("invalid arguments: %s", error)
        sys.exit(f"✗ {error}")
    plans = values.pop("plans", None)
    if not plans:
        logger.warning("invalid arguments: no keyword")
        sys.exit(
            '✗ --keyword is required: give at least one keyword, e.g. --keyword "plumbers".'
        )
    values["plans"] = [
        {
            key: value
            for key, value in plan.items()
            if value is not None and not key.startswith("_")
        }
        for plan in plans
    ]
    try:
        return SerperMapConfig.model_validate(values)
    except ValidationError as error:
        lines = []
        for problem in error.errors():
            field = next(
                (
                    str(part)
                    for part in reversed(problem["loc"])
                    if isinstance(part, str)
                ),
                "",
            )
            flag = FLAG_NAMES.get(field, f"--{field.replace('_', '-')}")
            if problem["type"] == "missing" or (
                problem["type"] == "string_type" and values.get(field) is None
            ):
                lines.append(f"✗ {flag} is required.")
            elif field == "keywords" and problem["type"] == "too_short":
                lines.append(f"✗ {flag} is required: give at least one keyword.")
            elif field:
                lines.append(f"✗ {flag}: {problem['msg']}.")
            else:
                lines.append(f"✗ {problem['msg']}.")
        lines = list(dict.fromkeys(lines))
        logger.warning("invalid arguments: %s", "; ".join(lines))
        sys.exit("\n".join(lines))


def load_api_keys() -> list[str]:
    """The API keys from ``SERPER_API_KEYS``, without blanks or repeats."""
    raw = os.getenv(KEY_VARIABLE, "")
    keys = list(dict.fromkeys(part.strip() for part in raw.split(",") if part.strip()))
    if not keys:
        sys.exit(
            f"✗ {KEY_VARIABLE} is empty: set it in .env to a comma-separated list of keys."
        )
    return keys


def print_plan(plan: SerperMapPlan, api_keys: list[str]) -> None:
    """Print what the run would do, and what it would cost. Never prints a key."""
    config = plan.config
    print("Dry run: nothing will be scraped or written.\n")
    print(
        f"Search: {', '.join(repr(word) for word in config.keywords)} in {config.place}"
    )
    for keyword_plan in config.plans:
        tiles = len(
            {
                (page.grid_x, page.grid_y)
                for page in plan.pages
                if page.keyword == keyword_plan.keyword
            }
        )
        where = (
            "whole city"
            if keyword_plan.grid == 1
            else f"{keyword_plan.grid}x{keyword_plan.grid} grid"
        )
        print(
            f"  {keyword_plan.keyword!r}: depth {keyword_plan.depth}, "
            f"{tiles} tiles, {where}"
        )
    print(f"Tiles: {plan.tile_count}")
    print(f"Requests: {plan.page_count} page(s)")
    print(f"Cost: {plan.estimated_credits} credits at 3 per page")
    print(f"Keys: {len(api_keys)} key(s), {config.parallel} parallel each")
    filters = [
        name
        for name, on in (("phone", config.has_phone), ("website", config.has_website))
        if on
    ]
    print(f"Filters: {', '.join(filters) or 'none'}")


def ask_resume(job: SerperMapJobInfo) -> bool:
    """Ask whether to continue an unfinished job of the same config."""
    print(
        f"\nAn unfinished job with this config exists: {job.name}\n"
        f"  id {job.id}, status {job.status}, last run {job.updated_at:%Y-%m-%d %H:%M}\n"
        f"  {job.done_requests} done, {job.pending_requests} pending, "
        f"{job.failed_requests} failed, {job.places_saved} places saved"
    )
    try:
        answer = input("Continue this job? [Y/n] ").strip().lower()
    except EOFError:
        return False
    return answer in ("", "y", "yes")


async def with_database[T](action: Callable[[], Awaitable[T]]) -> T:
    """Open Tortoise, run ``action``, and close the connections, all on one event loop."""
    await Tortoise.init(config=TORTOISE_ORM)
    try:
        return await action()
    finally:
        await Tortoise.close_connections()


def make_progress_printer() -> Callable[[SerperMapEvent], None]:
    """A one-line progress writer for the service's events."""

    def on_event(event: SerperMapEvent) -> None:
        if event.kind in ("request_done", "request_failed") and event.done is not None:
            mark = "✓" if event.kind == "request_done" else "✗"
            print(
                f"  {mark} page {event.page.page if event.page else '?'} "
                f"({event.done}/{event.total})"
            )
        elif event.kind in ("key_dry", "key_dead"):
            print(f"  ⚠ key #{event.key_index}: {event.detail}")

    return on_event


async def run(config: SerperMapConfig, api_keys: list[str], *, no_resume: bool) -> int:
    """Drive the service for one command; returns the process exit code."""
    service = SerperMapService(
        config, api_keys=api_keys, on_event=make_progress_printer()
    )
    try:
        job = None if no_resume else await service.find_unfinished()
        if job is not None and await asyncio.to_thread(ask_resume, job):
            job_id = await service.resume(job.id)
            print(f"▶ Continuing job {job_id}")
        else:
            job_id = await service.start()
            print(f"▶ Started job {job_id}")
        result = await service.run(job_id)
    finally:
        await service.aclose()
    return report(result)


def report(result: SerperMapResult) -> int:
    """Print the summary and return the exit code: 0 finished/exhausted, 1 failed."""
    print(
        f"\n{result.status}: {result.requests_done} page(s) done, "
        f"{result.requests_pending} pending, {result.requests_failed} failed\n"
        f"Places: {result.places_saved} saved of {result.places_found} found, "
        f"{result.credits_spent} credits spent"
    )
    if result.status == "exhausted":
        print(
            f"⚠ The API keys ran out of credits. The job {result.job_id} is saved; "
            "run the same command again to continue it."
        )
        return 0
    if result.status == "interrupted":
        print(f"⚠ Stopped. The job {result.job_id} is saved; run again to continue it.")
        return 130
    if result.status == "failed":
        print(f"✗ The run failed: {result.error or 'see the log above'}")
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    config = config_of(args)
    api_keys = load_api_keys()
    logger.info(
        "serper query: plans=%r place=%r parallel=%d "
        "has_phone=%s has_website=%s keys=%d dry_run=%s",
        [(plan.keyword, plan.depth, plan.grid) for plan in config.plans],
        config.place,
        config.parallel,
        config.has_phone,
        config.has_website,
        len(api_keys),
        args.dry_run,
    )
    if args.dry_run:
        try:
            plan = asyncio.run(build_plan(config))
        except SerperError as error:
            logger.error("the plan could not be built: %s", describe(error))
            sys.exit(f"✗ {describe(error)}")
        print_plan(plan, api_keys)
        return 0
    try:
        return asyncio.run(
            with_database(lambda: run(config, api_keys, no_resume=args.no_resume))
        )
    except KeyboardInterrupt:
        print("\n✗ Stopped.")
        return 130
    except SerperError as error:
        logger.error("the scrape failed: %s", describe(error))
        sys.exit(f"✗ {describe(error)}")


if __name__ == "__main__":
    sys.exit(main())
