"""Load one page in an isolated browser context and read its HTML."""

import urllib.parse
from typing import TYPE_CHECKING

from .constants import (
    ALLOWED_METHODS,
    ALLOWED_SCHEMES,
    BLOCKED_HTTP_STATUSES,
    BLOCKED_RESOURCE_TYPES,
    BLOCKED_TITLE_MARKERS,
    CONTACT_READY_SCRIPT,
    CONTACT_WAIT_TIMEOUT_MS,
    DOM_READY_TIMEOUT_MS,
    MAX_HTML_CHARS,
    NAVIGATION_TIMEOUT_MS,
)
from .deadline import milliseconds_left
from .render_result import RenderAttempt, RenderResult

if TYPE_CHECKING:
    from playwright.async_api import Browser, Page, Route


class PageRenderer:
    """Renders one URL in a fresh browser context.

    Images, media and fonts are aborted, as are non-GET/HEAD requests and
    non-HTTP schemes. Each context is closed when the render ends.

    :param browser: The running browser to create the context in.
    :param deadline: ``time.monotonic()`` value by which the page must finish.
    """

    def __init__(self, browser: "Browser", deadline: float) -> None:
        self._browser = browser
        self._deadline = deadline

    async def render(self, url: str, attempt: RenderAttempt) -> RenderResult:
        context = await self._browser.new_context(
            service_workers="block", accept_downloads=False
        )
        try:
            await context.route("**/*", _filter_request)
            page = await context.new_page()
            attempt.stage = "navigation"
            response = await page.goto(
                url,
                wait_until="commit",
                timeout=min(NAVIGATION_TIMEOUT_MS, milliseconds_left(self._deadline)),
            )
            if response and response.status >= 400:
                return attempt.failure(
                    "blocked" if response.status in BLOCKED_HTTP_STATUSES else "error"
                )
            attempt.stage = "rendering"
            await self._wait_for_dom_ready(page)
            await self._wait_for_contact_details(page)
            attempt.stage = "read_dom"
            title = (await page.title()).lower()
            if any(marker in title for marker in BLOCKED_TITLE_MARKERS):
                return attempt.failure("blocked")
            return {
                "html": (await page.content())[:MAX_HTML_CHARS],
                "status": "rendered",
                "stage": "complete",
            }
        finally:
            await context.close()

    async def _wait_for_dom_ready(self, page: "Page") -> None:
        from playwright.async_api import TimeoutError as PlaywrightTimeout

        try:
            await page.wait_for_load_state(
                "domcontentloaded",
                timeout=min(DOM_READY_TIMEOUT_MS, milliseconds_left(self._deadline)),
            )
        except PlaywrightTimeout:
            pass  # slow third-party scripts must not discard an already usable page

    async def _wait_for_contact_details(self, page: "Page") -> None:
        from playwright.async_api import TimeoutError as PlaywrightTimeout

        try:
            await page.wait_for_function(
                CONTACT_READY_SCRIPT,
                timeout=min(CONTACT_WAIT_TIMEOUT_MS, milliseconds_left(self._deadline)),
            )
        except PlaywrightTimeout:
            pass  # not every business publishes contact details


async def _filter_request(route: "Route") -> None:
    request = route.request
    if (
        request.resource_type in BLOCKED_RESOURCE_TYPES
        or request.method not in ALLOWED_METHODS
        or urllib.parse.urlsplit(request.url).scheme not in ALLOWED_SCHEMES
    ):
        await route.abort()
    else:
        await route.continue_()
