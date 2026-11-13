import argparse
import asyncio
import sys
from collections.abc import Awaitable, Callable

from pydantic import Field, ValidationError
from tortoise import Tortoise

import bootstrap.application  # noqa: F401 -- installs the central logging (config/logging.py)
from app.repositories import GoogleMapProfileRepository, LeadRepository
from app.services.google_map_scraper.place_query import PlaceQuery
from app.services.google_map_scraper.scrape.dedupe import seen_keys_of
from app.services.google_map_scraper.scrape.scrape_types import LeadRow
from app.services.google_map_scraper.scraper import PlaceScraper, columns_for
from app.utils.errors import ScraperError, describe
from app.utils.logger import get_logger
from config.redis import RedisConfig
from config.tortoise import TORTOISE_ORM, TortoiseConfig

logger = get_logger(__name__)

FLAG_NAMES = {"proxies": "--proxy"}


class CliQuery(PlaceQuery):
    """The flags of the CLI: the place search with the defaults and limits a terminal run uses."""

    fields: list[str] | None = Field(
        default=None,
        description="Columns to write, in this order. Names the scrape does not produce are written empty.",
    )
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


def field_names(text: str) -> list[str]:
    return comma_list(text, "field name")


def keyword_names(text: str) -> list[str]:
    return comma_list(text, "keyword")


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="Scrape Google Maps business listings.")
    ap.add_argument(
        "--keywords",
        type=keyword_names,
        required=True,
        metavar="KW,...",
        help='comma-separated business types to search for, e.g. "coffee shops,bakeries"',
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
        "--proxy",
        dest="proxies",
        action="append",
        metavar="URL",
        help="proxy for the engine: http://[user:pass@]host:port, https://, socks5:// or socks5h://host:port (no user:pass on SOCKS5); repeatable: the scrapes take turns through the proxies",
    )
    ap.add_argument("--depth", type=int, default=5)
    ap.add_argument(
        "--lang",
        default="en",
        metavar="XX",
        help="interface language the engine asks Google for, 2 letters (default: en; use the local language, e.g. pt, es, de)",
    )
    ap.add_argument(
        "--grid",
        type=int,
        metavar="N",
        help="split the --city area into N x N map tiles, one scrape each, deduped (breaks Google's ~120 per search cap; max 10)",
    )
    ap.add_argument(
        "--deep-scan",
        action="store_true",
        help="! slower: a whole-city run past Google's cap. Runs --grid (3 x 3 unless --grid says otherwise), then splits every tile that came back at the cap (about 20 rows) into 4 sub-tiles one zoom level in, deduped; up to 5x the tiles, about 25 s each",
    )
    ap.add_argument(
        "--max-time",
        dest="max_time",
        type=int,
        default=600,
        help="time limit in SECONDS for each scrape (default: 600, max 1200)",
    )
    ap.add_argument("--limit", type=int, metavar="N", help="stop at N rows")
    ap.add_argument(
        "--fields",
        type=field_names,
        metavar="NAME,...",
        help="comma-separated columns to write, in this order (default: all); a name the scrape does not produce is written empty",
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
    try:
        return CliQuery.model_validate(values)
    except ValidationError as error:
        lines = []
        for problem in error.errors():
            field = str(problem["loc"][0]) if problem["loc"] else ""
            flag = FLAG_NAMES.get(field, f"--{field.replace('_', '-')}")
            if problem["type"] == "missing":
                lines.append(f"✗ {flag} is required.")
            elif field:
                lines.append(f"✗ {flag}: {problem['msg']}.")
            else:
                lines.append(f"✗ {problem['msg']}.")
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
    tiles = (query.grid or 1) ** 2
    place = ", ".join(
        part for part in (query.city, query.state, query.country.upper()) if part
    )
    print("Dry run: nothing will be scraped or written.\n")
    keywords = ", ".join(repr(keyword) for keyword in query.keywords)
    print(f"Search: {keywords} in {place}")
    print(
        f"Tiles: {tiles}"
        + (
            " (deep scan adds more where Google caps results)"
            if query.deep_scan
            else ""
        )
    )
    print(
        f"Depth: {query.scroll_depth} batch(es) per keyword, {query.max_seconds}s per scrape"
    )
    print(f"Website: level {query.website_depth}")
    print(f"Proxies: {len(query.proxies or [])}")
    print(f"Row limit: {query.limit or 'none'}")
    print(f"Fields: {', '.join(query.fields) if query.fields else 'all'}")
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
        "query: keywords=%r city=%r state=%r country=%s grid=%s deep_scan=%s depth=%s max_seconds=%s limit=%s has_phone=%s has_website=%s dry_run=%s proxies=%d",
        query.keywords,
        query.city,
        query.state,
        query.country,
        query.grid,
        query.deep_scan,
        query.scroll_depth,
        query.max_seconds,
        query.limit,
        query.has_phone,
        query.has_website,
        query.dry_run,
        len(query.proxies or []),
    )

    if query.deep_scan:
        n = (query.grid or 3) ** 2
        print(
            f"\n!Deep scan: {n} tiles, and up to {4 * n} more where a tile is at Google's cap, one at a time, about 25 s each: expect {n * 25 // 60}-{5 * n * 25 // 60} min per city.\n"
        )

    if query.scroll_depth >= 10 or (query.grid or 1) ** 2 >= 4:
        print(
            "\nHeads-up: this is a large scrape (high depth or a grid).\n"
            "Running it back-to-back without proxies can get your IP temporarily rate-limited by Google (clears in minutes-hours; returns empty/failed scrapes meanwhile).\n"
            "For big or repeated runs, add proxies. Proceeding....\n",
        )

    known = set(columns_for(query))
    unknown = [name for name in query.fields or [] if name not in known]
    if unknown:
        logger.warning(
            "fields not produced by the scrape, written empty: %s", ", ".join(unknown)
        )
        print(
            f"⚠ --fields: not a column of the scrape, written empty: {', '.join(unknown)}\n"
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
