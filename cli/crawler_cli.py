import argparse
import dataclasses
import sys

import bootstrap.application  # noqa: F401 -- installs the central logging (config/logging.py)
from app.services.buffer.client import connect
from app.services.crawler.pipeline.run import crawl_one
from app.services.crawler.pipeline.stage import CRAWL_OPTIONS
from app.utils.errors import describe
from app.utils.logger import get_logger
from config.redis import RedisConfig
from config.tortoise import TortoiseConfig

logger = get_logger(__name__)


def validate_max_pages(value: int) -> int:
    if value < 1:
        raise argparse.ArgumentTypeError(f"--max-pages must be at least 1, not {value}")
    return value


def max_pages_of(text: str) -> int:
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"--max-pages must be a whole number, not {text!r}"
        ) from None
    return validate_max_pages(value)


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        description="Crawl one website and link it to its lead when one matches."
    )
    ap.add_argument("--website", required=True, help="website to crawl, e.g. a.de")
    ap.add_argument(
        "--max-pages",
        type=max_pages_of,
        default=100,
        metavar="N",
        help="pages to read per website (default: 100, at least 1)",
    )
    ap.add_argument(
        "--finish-incomplete",
        action="store_true",
        help="also crawl every other website still waiting in the shared queue, in order",
    )
    return ap


def main() -> None:
    args = build_parser().parse_args()
    options = dataclasses.replace(CRAWL_OPTIONS, max_pages=args.max_pages)
    try:
        summary = crawl_one(
            args.website,
            redis=connect(RedisConfig()),
            postgres=TortoiseConfig(),
            options=options,
            finish_incomplete=args.finish_incomplete,
        )
    except KeyboardInterrupt:
        print("\n⚠ Crawl stopped; unfinished crawls stay in Redis for the next run.")
        raise
    except Exception as exc:
        logger.exception("the crawl failed")
        sys.exit(f"✗ The crawl failed: {describe(exc)} (the traceback is above)")
    print(
        f"✓ Crawl: {summary.queued} queued, {summary.rejected} not crawlable, "
        f"{summary.skipped} skipped"
    )


def interrupted() -> str:
    return "\n✗ Stopped."


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(interrupted())
