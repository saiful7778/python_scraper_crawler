"""Tunables for website rendering. Durations are in seconds unless the name ends in ``_MS``."""

from typing import Final

#: Starting the browser with less time than this left wastes it and trips Node timers.
MIN_START_SECONDS: Final[float] = 3
#: Less time than this left is not worth starting a page or a wait.
MIN_REMAINING_SECONDS: Final[float] = 0.5
#: Time held back from each Playwright timeout so the browser can answer before the deadline.
LAUNCH_RESERVE_SECONDS: Final[float] = 0.2
#: Upper bound for each of ``browser.close()`` and ``playwright.stop()``. Close took 1.6-2.3 s
#: live and hung 24-300 s at times.
CLOSE_TIMEOUT_SECONDS: Final[float] = 5

DEFAULT_BUDGET_SECONDS: Final[float] = 30
DEFAULT_RENDER_SECONDS: Final[float] = 20
PAGE_TIME_LIMIT_SECONDS: Final[float] = 20

MAX_CONCURRENT_BROWSERS: Final[int] = 2
MAX_PAGES_PER_BROWSER: Final[int] = 2

NAVIGATION_TIMEOUT_MS: Final[int] = 10_000
DOM_READY_TIMEOUT_MS: Final[int] = 3_000
CONTACT_WAIT_TIMEOUT_MS: Final[int] = 2_000
MAX_HTML_CHARS: Final[int] = 400_000

CONNECT_TIMEOUT_SECONDS: Final[float] = 5
RELAY_CHUNK_BYTES: Final[int] = 65_536
RELAY_POLL_SECONDS: Final[float] = 0.2
PROXY_JOIN_SECONDS: Final[float] = 2

ALLOWED_SCHEMES: Final[tuple[str, ...]] = ("http", "https")
ALLOWED_METHODS: Final[tuple[str, ...]] = ("GET", "HEAD")
BLOCKED_RESOURCE_TYPES: Final[tuple[str, ...]] = ("image", "media", "font")
BLOCKED_HTTP_STATUSES: Final[tuple[int, ...]] = (401, 403, 429)
BLOCKED_TITLE_MARKERS: Final[tuple[str, ...]] = (
    "just a moment",
    "access denied",
    "verify you are human",
    "captcha",
)

BROWSER_ARGS: Final[list[str]] = [
    "--proxy-bypass-list=<-loopback>",
    "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
]

#: Headers the proxy drops when it forwards a plain request, since it sets its own.
HOP_BY_HOP_HEADERS: Final[frozenset[str]] = frozenset(
    {
        "host",
        "connection",
        "proxy-connection",
        "proxy-authorization",
        "content-length",
        "transfer-encoding",
    }
)

#: Run in the page; true once a mailto or social link, or an email address, is visible.
CONTACT_READY_SCRIPT: Final[str] = r"""() => document.body && (
    document.querySelector(`a[href^="mailto:"], a[href*="instagram.com/"],
    a[href*="facebook.com/"], a[href*="linkedin.com/"], a[href*="tiktok.com/"],
    a[href*="youtube.com/"], a[href*="twitter.com/"], a[href*="x.com/"]`) ||
    /[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}/i.test(document.body.innerText))"""
