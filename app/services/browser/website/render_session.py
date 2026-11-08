"""One headless Chrome that renders pages through a public-only proxy."""

import asyncio
import logging
import threading
import time
import urllib.parse
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from .availability import check_renderer_available
from .constants import (
    ALLOWED_SCHEMES,
    BROWSER_ARGS,
    CLOSE_TIMEOUT_SECONDS,
    MAX_CONCURRENT_BROWSERS,
    MAX_PAGES_PER_BROWSER,
    MIN_REMAINING_SECONDS,
    MIN_START_SECONDS,
    PAGE_TIME_LIMIT_SECONDS,
)
from .deadline import milliseconds_left, seconds_left
from .page_renderer import PageRenderer
from .public_proxy import PublicProxyServer
from .render_result import RenderAttempt, RenderResult

if TYPE_CHECKING:
    from playwright.async_api import Browser, Playwright

logger = logging.getLogger(__name__)


class BrowserSlots:
    """A process-wide cap on how many headless browsers run at once."""

    def __init__(self, count: int) -> None:
        self._semaphore = threading.BoundedSemaphore(count)

    def try_acquire(self) -> bool:
        return self._semaphore.acquire(blocking=False)

    def release(self) -> None:
        self._semaphore.release()


BROWSER_SLOTS = BrowserSlots(MAX_CONCURRENT_BROWSERS)


class RenderSession:
    """A headless browser, started on first use and shared by its pages.

    Pages render concurrently up to :data:`MAX_PAGES_PER_BROWSER`. The
    browser holds a :data:`BROWSER_SLOTS` slot from start until
    :meth:`close`. Must be used on the event loop that created it.

    :param is_public_ip: Predicate deciding whether an address is public.
    :param deadline: ``time.monotonic()`` value that bounds the session.
    """

    def __init__(self, is_public_ip: Callable[[str], bool], deadline: float) -> None:
        self.is_public_ip = is_public_ip
        self.deadline = deadline
        self._page_slots = asyncio.Semaphore(MAX_PAGES_PER_BROWSER)
        self._start_lock = asyncio.Lock()
        self._start_error: Exception | None = None
        self._has_slot = False
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._proxy: PublicProxyServer | None = None
        self._tasks: set[asyncio.Task] = set()

    async def start(self) -> None:
        async with self._start_lock:
            if self._browser is not None:
                return
            if self._start_error is not None:
                # One launch attempt per session; close() frees what it took.
                raise self._start_error
            try:
                if seconds_left(self.deadline) < MIN_START_SECONDS:
                    raise TimeoutError("browser budget exhausted")
                executable_path = check_renderer_available()
                self._has_slot = BROWSER_SLOTS.try_acquire()
                if not self._has_slot:
                    raise TimeoutError("browser capacity unavailable")
                from playwright.async_api import async_playwright

                self._proxy = PublicProxyServer(self.is_public_ip, self.deadline)
                proxy_url = self._proxy.start()
                self._playwright = await async_playwright().start()
                self._browser = await self._playwright.chromium.launch(
                    executable_path=executable_path,
                    headless=True,
                    timeout=milliseconds_left(self.deadline),
                    chromium_sandbox=True,
                    proxy={"server": proxy_url},
                    args=BROWSER_ARGS,
                )
            except Exception as error:
                self._start_error = error
                raise

    async def render(self, url: str) -> RenderResult:
        attempt = RenderAttempt()
        task = asyncio.current_task()
        assert task is not None
        self._tasks.add(task)
        acquired = False
        try:
            target = _parse_web_url(url)
            attempt.stage = "capacity"
            remaining = seconds_left(self.deadline)
            if remaining < MIN_REMAINING_SECONDS:
                return attempt.failure("budget_exhausted")
            await asyncio.wait_for(self._page_slots.acquire(), remaining)
            acquired = True
            attempt.stage = "browser_start"
            await self.start()
            assert self._browser is not None
            page_deadline = min(
                self.deadline, time.monotonic() + PAGE_TIME_LIMIT_SECONDS
            )
            page = PageRenderer(self._browser, page_deadline)
            return await page.render(target, attempt)
        except UnsupportedURLError:
            return attempt.failure("blocked")
        except Exception as error:
            # The message may hold URLs, credentials or page text, so only the
            # error type and the stage are logged.
            status = "timeout" if "timeout" in type(error).__name__.lower() else "error"
            logger.warning(
                "render failed at stage %s with %s (%s)",
                attempt.stage,
                type(error).__name__,
                status,
                exc_info=True,
            )
            return attempt.failure(status)
        finally:
            if acquired:
                self._page_slots.release()
            self._tasks.discard(task)

    async def close(self) -> None:
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if self._proxy is not None:
            # Nothing is served while the browser closes; its relays would otherwise run on
            # until the deadline.
            self._proxy.stop()
        try:
            try:
                if self._browser is not None:
                    # Chrome's graceful close can hang for minutes after many pages, so the
                    # driver is stopped below regardless; stopping it ends the browser too.
                    await _close_quietly(self._browser.close(), "browser")
            finally:
                if self._playwright is not None:
                    await _close_quietly(self._playwright.stop(), "playwright driver")
        finally:
            if self._has_slot:
                BROWSER_SLOTS.release()
                self._has_slot = False


class UnsupportedURLError(ValueError):
    """The URL is not an http(s) address without credentials."""


def _parse_web_url(url: str) -> str:
    """Return ``url`` as an http(s) URL with no credentials.

    A bare host is treated as HTTPS. The messages never echo the URL,
    because it may carry credentials.

    :raises UnsupportedURLError: If the scheme is not http(s), the host is
        missing, or credentials are present.
    """
    parsed = urllib.parse.urlsplit(url if "://" in url else "https://" + url)
    if parsed.scheme not in ALLOWED_SCHEMES:
        raise UnsupportedURLError(f"scheme {parsed.scheme!r} is not allowed")
    if not parsed.hostname:
        raise UnsupportedURLError("the URL has no host")
    if parsed.username or parsed.password:
        raise UnsupportedURLError("the URL contains credentials")
    return parsed.geturl()


async def _close_quietly(closing: Awaitable[object], what: str) -> None:
    try:
        await asyncio.wait_for(closing, CLOSE_TIMEOUT_SECONDS)
    except Exception:
        logger.warning("closing the %s failed", what, exc_info=True)
