import argparse
import asyncio
import sys
from collections.abc import Awaitable, Callable
from typing import cast

from pydantic import Field, ValidationError
from tortoise import Tortoise

import bootstrap.application  # noqa: F401 -- installs the central logging (config/logging.py)
from app.repositories import GoogleMapProfileRepository, LeadRepository
from app.services.google_map_scraper.place_query import PlaceQuery
from app.services.google_map_scraper.scrape.dedupe import seen_keys_of
from app.services.google_map_scraper.scraper import PlaceScraper
from app.utils.errors import ScraperError, describe
from app.utils.logger import get_logger
from config.tortoise import TORTOISE_ORM

logger = get_logger(__name__)

FLAG_NAMES = {
    "proxies": "--proxies",
    "keywords": "--keyword",
    "keyword_plans": "--keyword",
    "depth_to": "--depth-to",
}


class CliQuery(PlaceQuery):
    """The flags of the CLI: the place search with the defaults and limits a terminal run uses."""

    scroll_depth: int = Field(
        default=5,
        alias="depth",
        ge=1,
        le=10,
        strict=True,
        description="Batches of results to read per keyword.",
    )
    max_seconds: int = Field(
        default=600,
        alias="max_time",
        ge=1,
        le=1200,
        strict=True,
        description="Time limit in seconds for each scrape.",
    )
    dry_run: bool = Field(
        default=False, strict=True, description="Show the plan and exit."
    )


def comma_list(text: str, label: str) -> list[str]:
    """Split a comma-separated list, dropping blanks and repeats."""
    names = list(
        dict.fromkeys(part.strip() for part in text.split(",") if part.strip())
    )
    if not names:
        raise argparse.ArgumentTypeError(f"give at least one {label}")
    return names


def proxy_names(text: str) -> list[str]:
    return comma_list(text, "proxy")


class KeywordAction(argparse.Action):
    """Start a keyword plan: the --depth/--grid that follow belong to it."""

    def __call__(self, parser, namespace, values, option_string=None):
        plan = {"keyword": str(values), "depth": None, "depth_to": None, "grid": None}
        plans = getattr(namespace, "keyword_plans", None)
        if plans is None:
            plans = namespace.keyword_plans = []
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
        if self.dest == "depth_from":
            self._set_depth(
                plan, cast(int, values), namespace, option_string, ranged=True
            )
        elif self.dest == "depth":
            self._set_depth(
                plan, cast(int, values), namespace, option_string, ranged=False
            )
        elif self.dest == "depth_to":
            if plan.get("_depth_flag") and not plan.get("_ranged"):
                namespace.keyword_plan_error = (
                    f"{plan['_depth_flag']} sets one depth; give the shallowest "
                    "depth of a range with --depth-from before --depth-to"
                )
                return
            if not plan.get("_ranged"):
                namespace.keyword_plan_error = (
                    "--depth-to must follow a --depth-from flag"
                )
                return
            plan["depth_to"] = values
        else:
            plan[self.dest] = values

    @staticmethod
    def _set_depth(
        plan: dict,
        value: int,
        namespace: argparse.Namespace,
        option_string: str | None,
        *,
        ranged: bool,
    ) -> None:
        """Set a keyword's shallowest depth from --depth or --depth-from, once."""
        flag = option_string or ""
        if plan.get("_ranged"):
            namespace.keyword_plan_error = (
                f"--depth-to makes a depth range; set its shallowest depth with "
                f"--depth-from, not --depth (got {plan['_depth_flag']} and {flag})"
            )
            return
        if plan.get("_depth_flag"):
            namespace.keyword_plan_error = (
                f"{plan['_depth_flag']} and {flag} both set the shallowest depth of "
                "one keyword; give one of them"
            )
            return
        plan["depth"] = value
        plan["_depth_flag"] = flag
        plan["_ranged"] = ranged


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="Scrape Google Maps business listings.")
    ap.add_argument(
        "--keyword",
        "-k",
        action=KeywordAction,
        metavar="KEYWORD",
        help='a business type to search for, e.g. "coffee shops"; repeat it for more keywords, and give each its own --depth/--grid after it',
    )
    ap.add_argument("--city", required=True, help='city name, e.g. "Austin"')
    ap.add_argument("--state", help='optional state or region, e.g. "TX"')
    ap.add_argument(
        "--country",
        required=True,
        metavar="XX",
        help="country to search in, 2 letters (e.g. us, de, br)",
    )
    ap.add_argument(
        "--proxies",
        dest="proxies",
        action="extend",
        type=proxy_names,
        metavar="URL,...",
        help="comma-separated proxies for the engine: http://[user:pass@]host:port, https://, socks5:// or socks5h://host:port (no user:pass on SOCKS5); the scrapes take turns through the proxies",
    )
    ap.add_argument(
        "--depth",
        type=int,
        action=KeywordScopedAction,
        metavar="N",
        help="batches of results per keyword, 1 to 10 (default: 5); follows a --keyword flag",
    )
    ap.add_argument(
        "--depth-from",
        dest="depth_from",
        type=int,
        action=KeywordScopedAction,
        metavar="N",
        help="shallowest depth of a range of depths, 1 to 10: with --depth-to 8 the keyword searches depth 6, 7 and 8; follows a --keyword flag",
    )
    ap.add_argument(
        "--depth-to",
        dest="depth_to",
        type=int,
        action=KeywordScopedAction,
        metavar="N",
        help="deepest depth of a --depth-from range, 1 to 10 (default: the range's shallowest depth, so one pass); follows a --depth-from flag",
    )
    ap.add_argument(
        "--lang",
        default="en",
        metavar="XX",
        help="interface language the engine asks Google for, 2 letters (default: en; use the local language, e.g. pt, es, de)",
    )
    ap.add_argument(
        "--grid",
        type=int,
        action=KeywordScopedAction,
        metavar="N",
        help="split the --city area into N x N map tiles for that keyword, one scrape each, deduped (breaks Google's ~120 per search cap; default: 1, max 10); follows a --keyword flag",
    )
    ap.add_argument(
        "--max-time",
        dest="max_time",
        type=int,
        default=600,
        help="time limit in SECONDS for each scrape's Google Maps work (default: 600, max 1200). Each keyword, and each grid tile, is its own scrape.",
    )
    ap.add_argument(
        "--limit",
        type=int,
        metavar="N",
        help="stop each scrape at N rows (each keyword, and each grid tile, has its own limit)",
    )
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="check the arguments and show the plan, without scraping or writing a CSV",
    )

    f = ap.add_argument_group("filters", "keep only the rows that have the field")
    f.add_argument(
        "--has-phone", action="store_true", help="keep only rows with a phone number"
    )
    f.add_argument(
        "--has-website",
        action="store_true",
        help="keep only rows where a website was found",
    )
    return ap


def read_query() -> CliQuery:
    """Parse the command line into a validated query; exits with the reason if it is invalid."""
    values = {
        k: v for k, v in vars(build_parser().parse_args()).items() if v is not None
    }
    values.pop("keyword_scope", None)
    error = values.pop("keyword_plan_error", None)
    if error:
        logger.warning("invalid arguments: %s", error)
        sys.exit(f"✗ {error}")
    plans = values.pop("keyword_plans", None)
    if not plans:
        logger.warning("invalid arguments: no keyword")
        sys.exit(
            '✗ --keyword is required: give at least one, e.g. --keyword "coffee shops".'
        )
    values["keyword_plans"] = [
        {
            key: value
            for key, value in plan.items()
            if value is not None and not key.startswith("_")
        }
        for plan in plans
    ]
    if values.get("proxies"):
        values["proxies"] = list(dict.fromkeys(values["proxies"]))
    try:
        return CliQuery.model_validate(values)
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
            if problem["type"] == "missing":
                lines.append(f"✗ {flag} is required.")
            elif field:
                lines.append(f"✗ {flag}: {problem['msg']}.")
            else:
                lines.append(f"✗ {problem['msg']}.")
        # A keyword is validated as a plan and again as the derived keyword tuple,
        # so the same flag can fail twice: report each flag once, first problem.
        lines = list(dict.fromkeys(lines))
        first_by_flag: dict[str, str] = {}
        for line in lines:
            flag = line.split(":")[0]
            first_by_flag.setdefault(flag, line)
        lines = list(first_by_flag.values())
        logger.warning("invalid arguments: %s", "; ".join(lines))
        sys.exit("\n".join(lines))


async def with_database[T](action: Callable[[], Awaitable[T]]) -> T:
    """Open Tortoise, run ``action``, and close the connections, all on one event loop."""
    await Tortoise.init(config=TORTOISE_ORM)
    try:
        return await action()
    finally:
        await Tortoise.close_connections()


async def load_known_keys() -> frozenset[str]:
    logger.debug("reading the known lead keys from PostgreSQL")
    repository = GoogleMapProfileRepository(LeadRepository())
    keys = frozenset(seen_keys_of(await repository.key_rows()))
    logger.info("loaded %d known lead keys", len(keys))
    return keys


def print_plan(query: CliQuery) -> None:
    place = ", ".join(
        part for part in (query.city, query.state, query.country.upper()) if part
    )
    print("Dry run: nothing will be scraped or written.\n")
    keywords = ", ".join(repr(plan.keyword) for plan in query.keyword_plans)
    print(f"Search: {keywords} in {place}")
    for plan in query.keyword_plans:
        passes = len(plan.depths)
        tiles = plan.grid**2 * passes
        where = "whole city" if plan.grid == 1 else f"{plan.grid}x{plan.grid} grid"
        span = (
            f"depth {plan.depth}"
            if plan.depth_to is None
            else f"depth {plan.depth}-{plan.depth_to} ({passes} passes per tile)"
        )
        print(f"  {plan.keyword!r}: {span}, {tiles} scrape(s), {where}")
    print(
        f"Scrapes: {sum(len(plan.depths) * plan.grid**2 for plan in query.keyword_plans)}"
    )
    print(f"Max time: {query.max_seconds}s per scrape")
    print(f"Website: level {query.website_depth}")
    print(f"Proxies: {len(query.proxies or [])}")
    row_limit = f"{query.limit} per scrape" if query.limit else "none"
    print(f"Row limit: {row_limit}")
    filters = [
        name
        for name, on in (
            ("phone", query.has_phone),
            ("website", query.has_website),
        )
        if on
    ]
    print(f"Filters: {', '.join(filters) or 'none'}")


def main() -> None:
    query = read_query()
    logger.info(
        "query: plans=%r city=%r state=%r country=%s max_seconds=%s limit=%s has_phone=%s has_website=%s dry_run=%s proxies=%d",
        [
            (plan.keyword, plan.depth, plan.depth_to, plan.grid)
            for plan in query.keyword_plans
        ],
        query.city,
        query.state,
        query.country,
        query.max_seconds,
        query.limit,
        query.has_phone,
        query.has_website,
        query.dry_run,
        len(query.proxies or []),
    )

    if any(max(plan.depths) >= 10 or plan.grid >= 2 for plan in query.keyword_plans):
        print(
            "\nHeads-up: this is a large scrape (high depth or a grid).\n"
            "Running it back-to-back without proxies can get your IP temporarily rate-limited by Google (clears in minutes-hours; returns empty/failed scrapes meanwhile).\n"
            "For big or repeated runs, add proxies. Proceeding....\n",
        )

    if query.dry_run:
        logger.info("dry run: plan shown, nothing scraped or written")
        print_plan(query)
        return

    try:
        known_keys = asyncio.run(with_database(load_known_keys))
    except Exception as exc:  # noqa: BLE001 - any database failure should exit cleanly
        sys.exit(
            f"✗ Could not read the existing leads from the database: {describe(exc)}"
        )
    print(f"▶ {len(known_keys)} known lead keys loaded; duplicates are skipped\n")

    scraper = PlaceScraper()
    try:
        result = scraper.scrape(query, known_keys)
    except KeyboardInterrupt:
        salvage = scraper.salvage_rows
        logger.warning("scrape interrupted with %d leads found so far", len(salvage))
        raise
    except ScraperError as exc:
        logger.error("scrape failed: %s", describe(exc))
        sys.exit(f"✗ {describe(exc)}")
    except (ValueError, RuntimeError) as exc:
        logger.exception("the scrape failed unexpectedly")
        sys.exit(f"✗ {describe(exc)} (the traceback is above)")

    logger.info(
        "scrape done: %d finished, %d skipped, blocked=%s, %d leads",
        result.finished,
        result.skipped,
        result.blocked,
        len(result.rows),
    )
    if result.blocked:
        logger.warning(
            "Google rate-limited this IP; the rows found before that are kept"
        )
        print("⚠ Google rate-limited this IP. The rows found before that are kept.")
    if result.skipped:
        logger.warning("%d scrape(s) skipped", result.skipped)
        print(f"⚠ {result.skipped} scrape(s) skipped; see the ⚠ lines above.")
    if result.finished == 0:
        logger.error("no scrape finished")
        sys.exit("✗ No scrape finished; the reason is in the ✗ line above.")


def interrupted() -> str:
    return "\n✗ Stopped."


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(interrupted())
