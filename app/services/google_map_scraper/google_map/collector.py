"""Scroll a Google Maps search feed and collect place leads.

The feed renders into ``div[role="feed"]``; scrolling it makes Maps fetch
further result batches as JSON. This module drives that loop and returns
whatever it gathered, along with a short reason string and counters.

The algorithm, in order:

1. Install consent cookies and dismiss the interstitial if it still
   appears.
2. Navigate and confirm the feed rendered (or that the query collapsed
   to a single place).
3. Scroll, polling :class:`batches.BatchResponseReader` for new batches.
4. Stop on end-of-list, on a Google block (``/sorry/`` or HTTP 429), on
   ``depth`` batches, on ``max_seconds``, or when ``stop()`` returns true.
5. If nothing was read from batches, fall back to scraping DOM anchors
   (Google changed the batch format).

The scroll timing is subtle: after scrolling, the page either asks
Google for the next batch within :data:`selectors.SETTLE_SECONDS` or
never does (the list is complete, even without an end line). Once a
request is in flight, up to :data:`selectors.SLOW_BATCH_SECONDS` is
allowed for its answer. While the feed is still rendering the last batch
(its scrollHeight grows), the bottom has not been reached, so the settle
clock restarts.
"""

import time
from collections.abc import Callable
from typing import Final, cast

from app.services.browser import BrowserTab, CDPError
from app.services.google_map_scraper.extract_details import PlaceRecord
from app.utils.errors import describe
from app.utils.logger import get_logger

from .batches import BatchResponseReader
from .parsing import (
    BatchStats,
    PlaceLink,
    feed_is_exhausted,
    parse_place_href,
)
from .selectors import (
    CONSENT_COOKIES,
    FEED_GROWTH_POLL_SECONDS,
    JS_DISMISS_CONSENT,
    JS_FEED_END_STATE,
    JS_FEED_LINKS,
    JS_FEED_PRESENT,
    JS_SCROLL_FEED_TO_BOTTOM,
    SETTLE_SECONDS,
    SLOW_BATCH_SECONDS,
)

logger = get_logger(__name__)

#: Default number of result batches to read.
_DEFAULT_DEPTH: Final[int] = 5

#: Default wall-clock budget for collection, in seconds.
_DEFAULT_MAX_SECONDS: Final[float] = 180.0

#: How long to wait for the very first batch, which arrives with the page.
_FIRST_BATCH_TIMEOUT_SECONDS: Final[float] = 10.0

#: How long to wait for the feed, or for the consent interstitial to go.
_FEED_TIMEOUT_SECONDS: Final[float] = 15.0


def collect_leads(
    tab: BrowserTab,
    url: str,
    *,
    depth: int = _DEFAULT_DEPTH,
    max_seconds: float = _DEFAULT_MAX_SECONDS,
    stop: Callable[[], bool] = lambda: False,
    on_batch: Callable[[list[PlaceRecord], int], None] | None = None,
) -> tuple[list[PlaceLink | PlaceRecord], str, BatchStats]:
    """Scroll a Maps search feed and collect leads until a stop condition.

    :param tab: The tab to drive; it is navigated to ``url``.
    :param url: A Google Maps search URL (see
        :func:`parsing.build_maps_search_url`).
    :param depth: Maximum number of result batches to read.
    :param max_seconds: Wall-clock budget for the whole collection.
    :param stop: Polled between steps; when it returns true, collection
        stops with reason ``"stopped"``.
    :param on_batch: Optional callback invoked after each batch with the
        newly seen rows and the running batch count.
    :returns: A ``(leads, reason, stats)`` triple. ``reason`` is one of
        ``"depth"``, ``"end-of-list"``, ``"blocked"``, ``"single"``,
        ``"no-feed"``, ``"no-growth"``, ``"stopped"``, ``"timeout"``, or
        an ``"error: ..."`` string when a mid-scroll failure still
        yielded leads. ``stats`` counts batches read and DOM links
        scraped.
    :raises CDPError: If the connection is lost and no leads were
        collected.
    """
    for name, value in cast(dict[str, str], CONSENT_COOKIES).items():
        tab.set_secure_cookie(name, value, ".google.com")

    reader = BatchResponseReader(tab)
    seen: dict[str, PlaceRecord | PlaceLink] = {}
    started = time.monotonic()
    reason = "depth"
    stale = 0
    try:
        tab.navigate(url)
        if "/sorry/" in tab.get_url():
            return [], "blocked", {"batches": 0, "dom_links": 0}
        if "consent.google" in tab.get_url():
            tab.evaluate(JS_DISMISS_CONSENT)
            if not tab.wait_for_truthy(
                "!location.href.includes('consent.google')",
                timeout=_FEED_TIMEOUT_SECONDS,
            ):
                logger.warning(
                    "the consent page did not clear within %s s: %s",
                    _FEED_TIMEOUT_SECONDS,
                    tab.get_url(),
                )
        if not tab.wait_for_truthy(JS_FEED_PRESENT, timeout=_FEED_TIMEOUT_SECONDS):
            if "/maps/place/" in tab.get_url():
                # The query resolved to a single place, not a list.
                return (
                    [parse_place_href(tab.get_url())],
                    "single",
                    {"batches": 0, "dom_links": 1},
                )
            logger.warning(
                "the results feed did not render within %s s; page: %s",
                _FEED_TIMEOUT_SECONDS,
                tab.get_url(),
            )
            return [], "no-feed", {"batches": 0, "dom_links": 0}

        def take() -> int:
            """Read any newly available batches into ``seen``; return count."""
            new_rows: list[PlaceRecord] = []
            for row in reader.poll():
                key = (
                    row.get("data_id")
                    or row.get("place_id")
                    or row.get("title")
                    or repr(row)
                )
                if key not in seen:
                    seen[key] = row
                    new_rows.append(row)
            if new_rows and on_batch:
                on_batch(new_rows, reader.count)
            return len(new_rows)

        try:
            # The first batch arrives with the page itself.
            first_batch_deadline = time.monotonic() + _FIRST_BATCH_TIMEOUT_SECONDS
            while (
                time.monotonic() < first_batch_deadline
                and reader.count == 0
                and not reader.limited
            ):
                take()
                time.sleep(FEED_GROWTH_POLL_SECONDS)
            logger.info("  batch %s: %s leads", reader.count, len(seen))

            while (
                reader.count < depth
                and time.monotonic() - started < max_seconds
                and not stop()
            ):
                if feed_is_exhausted(tab.evaluate(JS_FEED_END_STATE)):
                    reason = "end-of-list"
                    break
                if reader.limited or "/sorry/" in (tab.get_url() or ""):
                    reason = "blocked"
                    break

                count_before = reader.count
                asked_before = reader.asked
                height_before = tab.evaluate(JS_SCROLL_FEED_TO_BOTTOM)
                scrolled_at = time.monotonic()
                settle_deadline = scrolled_at + SETTLE_SECONDS
                while (
                    time.monotonic() < settle_deadline
                    and reader.count == count_before
                    and not stop()
                ):
                    time.sleep(FEED_GROWTH_POLL_SECONDS)
                    take()
                    if reader.inflight:
                        settle_deadline = scrolled_at + SLOW_BATCH_SECONDS
                    elif reader.count == count_before:
                        if reader.asked > asked_before and not reader.pending:
                            # Answered (or failed), nothing new.
                            break
                        height = tab.evaluate(JS_SCROLL_FEED_TO_BOTTOM)
                        if height != height_before:
                            height_before = height
                            scrolled_at = time.monotonic()
                            settle_deadline = scrolled_at + SETTLE_SECONDS

                if reader.count == count_before:
                    if reader.limited:
                        # Google refused the next batch: stop asking.
                        reason = "blocked"
                        break
                    stale += 1
                    if reader.asked == asked_before:
                        # Two scrolls that asked Google for nothing:
                        # the list is complete.
                        if stale >= 2:
                            reason = "end-of-list"
                            break
                    elif stale >= 2:
                        # Google answered twice with nothing new, or is
                        # slower than SLOW_BATCH_SECONDS.
                        reason = "no-growth"
                        break
                else:
                    stale = 0
                    logger.info("  batch %s: %s leads", reader.count, len(seen))
            else:
                if stop():
                    reason = "stopped"
                elif time.monotonic() - started >= max_seconds:
                    reason = "timeout"
                elif reader.count == 0 and reader.limited:
                    reason = "blocked"

            take()
            dom_links: list[dict[str, str]] = tab.evaluate(JS_FEED_LINKS) or []

        except CDPError as error:
            logger.exception(
                "collecting leads from %s stopped after %d batches",
                url,
                reader.count,
            )
            if not seen:
                raise
            return (
                list(seen.values()),
                f"error: {describe(error)}",
                {"batches": reader.count, "dom_links": 0},
            )

        if not seen and reason != "blocked":
            # The batch format changed under us: fall back to the page.
            for anchor in dom_links:
                place = parse_place_href(anchor["href"], anchor["label"])
                seen.setdefault(place["data_id"] or place["link"], place)
            reason = f"{reason}+dom-fallback"

        return (
            list(seen.values()),
            reason,
            {"batches": reader.count, "dom_links": len(dom_links)},
        )
    finally:
        reader.close()
