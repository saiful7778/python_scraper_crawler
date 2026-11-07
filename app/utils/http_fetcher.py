"""Plain-HTTP page fetching with SSRF protection and a hard time limit.

The fetcher never uses an environment proxy, refuses non-public peers (also
after redirects) and cuts a stalled connection after ``SITE_HARD`` seconds even
when the server keeps the socket open.
"""

import http.client
import socket
import threading
import time
import urllib.parse
import urllib.request
from typing import Self

from .network_guard import create_public_connection, resolves_only_to_public_addresses
from .url_utils import encode_url_as_ascii

_MAX_PAGE_BYTES = 400_000
SITE_TIMEOUT = 10  # seconds per website request (each socket read)
SITE_HARD = 30  # seconds a website request may take in all, then its socket is closed
UA = "Mozilla/5.0 (compatible; python_scraper_crawler-scraper/3.0)"


class FetchDeadline:
    """Per-thread hard-stop timer for the connection currently in progress."""

    _local = threading.local()

    @classmethod
    def begin_window(cls) -> None:
        """Start a window of ``SITE_HARD`` seconds for this thread's fetch."""
        cls._local.deadline = time.monotonic() + SITE_HARD

    @classmethod
    def end_window(cls) -> None:
        """Cancel the timer and clear this thread's deadline."""
        cls.cancel_timer()
        cls._local.deadline = None

    @classmethod
    def arm(cls, connection: http.client.HTTPConnection) -> None:
        """Schedule a socket shutdown that wakes a blocked read when time is up.

        The socket is captured now because urllib sends ``Connection: close``,
        so the connection object drops its reference once the response starts.
        The fetching thread closes the socket itself; closing it here could
        wait on the socket's buffer lock.
        """
        sock = connection.sock

        def shut_down() -> None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

        cls.cancel_timer()
        deadline = getattr(cls._local, "deadline", None)
        delay = max(0.1, deadline - time.monotonic()) if deadline else SITE_HARD
        timer = threading.Timer(delay, shut_down)
        timer.daemon = True
        timer.start()
        cls._local.timer = timer

    @classmethod
    def cancel_timer(cls) -> None:
        """Cancel this thread's pending hard-stop timer, if any."""
        timer = getattr(cls._local, "timer", None)
        if timer is not None:
            timer.cancel()
            cls._local.timer = None


class FetchWindow:
    """Context manager that bounds one fetch with :class:`FetchDeadline`."""

    def __enter__(self) -> Self:
        FetchDeadline.begin_window()
        return self

    def __exit__(self, *exc_info) -> None:
        FetchDeadline.end_window()


def read_response_capped(response, max_bytes: int, max_seconds: float) -> bytes:
    """Read up to ``max_bytes`` from ``response`` within ``max_seconds``.

    A cut-off or torn-down connection simply ends the read with what arrived.
    """
    received = bytearray()
    end = time.monotonic() + max_seconds
    while len(received) < max_bytes and time.monotonic() < end:
        try:
            chunk = response.read1(min(1 << 14, max_bytes - len(received)))
        except (OSError, ValueError, http.client.HTTPException):
            break
        if not chunk:
            break
        received += chunk
    return bytes(received)


class PublicOnlyHTTPConnection(http.client.HTTPConnection):
    """HTTP connection that refuses non-public peers and arms the hard stop."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._create_connection = create_public_connection

    def connect(self) -> None:
        """Connect, then start the hard-stop timer."""
        super().connect()
        FetchDeadline.arm(self)


class PublicOnlyHTTPSConnection(http.client.HTTPSConnection):
    """HTTPS connection that refuses non-public peers and arms the hard stop."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._create_connection = create_public_connection

    def connect(self) -> None:
        """Connect, then start the hard-stop timer."""
        super().connect()
        FetchDeadline.arm(self)


class PublicOnlyHTTPHandler(urllib.request.HTTPHandler):
    """urllib handler that opens :class:`PublicOnlyHTTPConnection`."""

    def http_open(self, req):
        return self.do_open(PublicOnlyHTTPConnection, req)


class PublicOnlyHTTPSHandler(urllib.request.HTTPSHandler):
    """urllib handler that opens :class:`PublicOnlyHTTPSConnection`."""

    _context = None

    def https_open(self, req):
        return self.do_open(PublicOnlyHTTPSConnection, req, context=self._context)


class PublicOnlyRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Follows redirects only to http(s) URLs whose host is public."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        """Return the follow-up request, or ``None`` to refuse the redirect."""
        parts = urllib.parse.urlsplit(newurl)
        if parts.scheme not in ("http", "https"):
            return None  # urllib's ftp handler would connect past the peer check
        if not resolves_only_to_public_addresses(parts.hostname or ""):
            return None
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class WebPageFetcher:
    """Downloads the HTML of public web pages."""

    def __init__(self):
        """Build an opener that ignores environment proxies."""
        self._opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}),
            PublicOnlyHTTPHandler,
            PublicOnlyHTTPSHandler,
            PublicOnlyRedirectHandler,
        )

    def fetch(self, url: str, timeout: float = SITE_TIMEOUT) -> str:
        """Return the page text, or ``""`` when it cannot be fetched.

        A missing scheme defaults to ``http://``.  Non-public hosts, errors and
        timeouts all yield an empty page; at most 400 kB are read.
        """
        if not url.startswith(("http://", "https://")):
            url = "http://" + url
        try:
            if not resolves_only_to_public_addresses(
                urllib.parse.urlsplit(url).hostname or ""
            ):
                return ""
            request = urllib.request.Request(
                encode_url_as_ascii(url),
                headers={"User-Agent": UA, "Accept": "text/html"},
            )
            with FetchWindow(), self._opener.open(request, timeout=timeout) as response:
                body = read_response_capped(response, _MAX_PAGE_BYTES, 2 * timeout)
                return body.decode("utf-8", "replace")
        except Exception:  # noqa: BLE001 - a failed fetch is an empty page
            return ""


_DEFAULT_FETCHER = WebPageFetcher()


def fetch_web_page(url: str, timeout: float = SITE_TIMEOUT) -> str:
    """Fetch a page with the shared default :class:`WebPageFetcher`."""
    return _DEFAULT_FETCHER.fetch(url, timeout)
