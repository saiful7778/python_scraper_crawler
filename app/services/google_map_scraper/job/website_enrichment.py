"""Finds emails and social profiles on each lead's website."""

import concurrent.futures
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Final, Protocol, cast

from app.services.browser import RenderStatus, merge_render_status
from app.services.browser.website.render_budget import RenderBudget
from app.utils.contacts import find_emails, find_socials
from app.utils.errors import describe
from app.utils.http_fetcher import SITE_TIMEOUT, fetch_web_page
from app.utils.network_guard import is_public_ip_address
from app.utils.url_utils import extract_site_host

from .job_limits import RENDER_MAX_SECONDS, RENDER_WORKERS, WEBSITE_WORKERS
from .job_types import LeadRow, is_lead_key
from .lead_rows import SOCIALS

logger = logging.getLogger(__name__)

STATUS_COLUMN: Final = "website_fetch_status"
_TIMED_OUT_STATUS: RenderStatus = "timeout"
_BUDGET_EXHAUSTED_STATUS: RenderStatus = "budget_exhausted"


class PageFetcher(Protocol):
    """Plain-HTTP page download: the URL and a timeout in seconds, returning the HTML."""

    def __call__(self, url: str, *, timeout: float) -> str: ...


@dataclass(frozen=True)
class WebsiteEnrichment:
    """What the website pass achieved.

    ``rows_enriched`` counts leads that gained a contact field; ``unchecked``
    counts websites the plain-HTTP pass did not reach in time.
    """

    rows_enriched: int
    unchecked: int


@dataclass(frozen=True)
class ContactOptions:
    """Which contact fields the website pass should look for."""

    emails: bool
    socials: bool
    use_browser: bool


def contact_fields(page: str, website: str, options: ContactOptions) -> dict[str, str]:
    """Return the non-empty contact columns found on one page."""
    if not page:
        return {}
    fields: dict[str, str] = {}
    if options.emails:
        emails = find_emails(page, extract_site_host(website))
        if emails:
            fields["emails"] = ", ".join(emails)
    if options.socials:
        fields.update({k: v for k, v in find_socials(page).items() if v})
    return fields


def lacks_contacts(row: LeadRow, options: ContactOptions) -> bool:
    """True when a wanted contact field is still empty on the row."""
    return (options.emails and not row.get("emails")) or (
        options.socials and not any(row.get(k) for k in SOCIALS)
    )


def _merge_status(row: LeadRow, incoming: RenderStatus) -> str:
    current = cast(RenderStatus, row.get(STATUS_COLUMN, "error"))
    return merge_render_status(current, incoming)


class WebsiteEnricher:
    """Fills in emails and social links from each lead's website."""

    def __init__(self, fetch_page: PageFetcher = fetch_web_page):
        """Use ``fetch_page`` for plain-HTTP requests (replaceable in tests)."""
        self._fetch_page = fetch_page

    def enrich(
        self,
        rows: list[LeadRow],
        seconds: float,
        options: ContactOptions,
        should_stop: Callable[[], bool],
    ) -> WebsiteEnrichment:
        """Update ``rows`` in place within ``seconds``; return what was achieved."""
        end = time.monotonic() + seconds
        websites = [row for row in rows if row.get("website")]
        enriched: set[int] = set()

        completed, pending = self._run_pool(
            websites,
            lambda row: self._fetch_homepage(row, options, end),
            WEBSITE_WORKERS,
            end,
            should_stop,
            _TIMED_OUT_STATUS if options.use_browser else None,
            enriched,
        )

        if options.use_browser and not should_stop():
            self._render_missing(completed, options, end, should_stop, enriched)

        return WebsiteEnrichment(rows_enriched=len(enriched), unchecked=pending)

    def _fetch_homepage(
        self, row: LeadRow, options: ContactOptions, end: float
    ) -> dict[str, str]:
        timeout = min(SITE_TIMEOUT, max(1, int(end - time.monotonic())))
        page = self._fetch_page(row["website"], timeout=timeout)
        found = contact_fields(page, row["website"], options)
        if options.use_browser:
            found[STATUS_COLUMN] = "http" if found else "error"
        return found

    def _render_missing(
        self,
        completed: list[LeadRow],
        options: ContactOptions,
        end: float,
        should_stop: Callable[[], bool],
        enriched: set[int],
    ) -> None:
        missing = [row for row in completed if lacks_contacts(row, options)]
        seconds = max(0.0, min(RENDER_MAX_SECONDS, end - time.monotonic()))
        with RenderBudget(seconds) as budget:

            def render(row: LeadRow) -> dict[str, str]:
                rendered = budget.fetch(row["website"], is_public_ip_address)
                found = contact_fields(rendered["html"], row["website"], options)
                found[STATUS_COLUMN] = _merge_status(row, rendered["status"])
                return found

            self._run_pool(
                missing,
                render,
                RENDER_WORKERS,
                min(end, time.monotonic() + RENDER_MAX_SECONDS),
                should_stop,
                _BUDGET_EXHAUSTED_STATUS,
                enriched,
            )

    @staticmethod
    def _run_pool(
        rows: list[LeadRow],
        work: Callable[[LeadRow], dict[str, str]],
        workers: int,
        deadline: float,
        should_stop: Callable[[], bool],
        timeout_status: RenderStatus | None,
        enriched: set[int],
    ) -> tuple[list[LeadRow], int]:
        """Run ``work`` on each row until done, the deadline passes or ``should_stop``.

        Returns the rows that finished and how many were still pending.
        """
        pool = concurrent.futures.ThreadPoolExecutor(workers)
        futures = {pool.submit(work, row): row for row in rows}
        pending = set(futures)
        while pending and not should_stop() and time.monotonic() < deadline:
            _, pending = concurrent.futures.wait(
                pending,
                timeout=min(1, max(0.0, deadline - time.monotonic())),
            )
        if timeout_status:
            for future in pending:
                row = futures[future]
                row[STATUS_COLUMN] = _merge_status(row, timeout_status)
        pool.shutdown(wait=False, cancel_futures=True)

        completed: list[LeadRow] = []
        for future in set(futures) - pending:
            row = futures[future]
            error = future.exception()
            if error is not None:
                logger.warning(
                    "website %s could not be read: %s",
                    row.get("website", ""),
                    describe(error),
                    exc_info=error,
                )
                continue
            found = {k: v for k, v in future.result().items() if v}
            for key, value in found.items():
                if is_lead_key(key):
                    row[key] = value
            completed.append(row)
            if any(k != STATUS_COLUMN for k in found):
                enriched.add(id(row))
        return completed, len(pending)
