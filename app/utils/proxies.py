"""Proxy URL validation, credential masking and rotation."""

import urllib.parse
from collections.abc import Sequence
from typing import Any

from app.utils.errors import ProxyConfigError, describe

PROXY_SCHEMES = ("http", "https", "socks5", "socks5h")

_PROXY_FORMAT_HELP = "use http://, https://, socks5:// or socks5h://host:port"


def parse_proxy_urls(value: Any) -> list[str]:
    """Validate a user-supplied proxy list and return the stripped URLs.

    Raises:
        ValueError: if the value is not a list of at most 100 proxy URLs with
            an allowed scheme, a host and a valid port, or if a SOCKS5 proxy
            carries credentials (Chrome cannot authenticate to SOCKS5).
    """
    if value in (None, ""):
        return []
    if (
        not isinstance(value, list)
        or len(value) > 100
        or not all(isinstance(item, str) for item in value)
    ):
        raise ProxyConfigError("invalid proxies: a list of up to 100 proxy URLs")
    # Messages give the entry's position only: the URL may hold a password.
    for position, proxy in enumerate(value, start=1):
        try:
            parts = urllib.parse.urlsplit(proxy.strip())
            parts.port  # noqa: B018 - raises on a bad port
        except ValueError as error:
            raise ProxyConfigError(
                f"invalid proxy {position}: {_PROXY_FORMAT_HELP} ({describe(error)})"
            ) from error
        if parts.scheme not in PROXY_SCHEMES or not parts.hostname:
            raise ProxyConfigError(f"invalid proxy {position}: {_PROXY_FORMAT_HELP}")
        if (parts.username or parts.password) and parts.scheme.startswith("socks"):
            raise ProxyConfigError(
                f"invalid proxy {position}: Chrome cannot log in to a SOCKS5 proxy; use its http:// address "
                "with user:pass, or one that allows your IP address"
            )
    return [proxy.strip() for proxy in value]


def mask_proxy_credentials(proxy: str) -> str:
    """Hide the password of a proxy URL so it can be shown in the scrape list.

    A malformed URL (possible for scrapes created by older versions) is fully
    masked instead of raising.
    """
    try:
        parts = urllib.parse.urlsplit(proxy)
    except ValueError:
        return "***"
    if not (parts.username or parts.password):
        return proxy
    userinfo, _, host = parts.netloc.rpartition("@")
    shown = userinfo.partition(":")[0] + ":***" if parts.password else "***"
    return urllib.parse.urlunsplit(parts._replace(netloc=f"{shown}@{host}"))


class ProxyRotation:
    """Chooses which proxy serves each keyword and remembers failed ones.

    The proxy for keyword ``n`` is the ``n``-th in turn, shifted by the number
    of times the rotation was advanced, skipping proxies marked as failed.
    When every proxy failed the in-turn proxy is used anyway.
    """

    def __init__(self, proxies: Sequence[str]):
        """Create a rotation over ``proxies`` (may be empty)."""
        self._proxies = tuple(proxies)
        self._shift = 0
        self._failed: set[str | None] = set()

    def __len__(self) -> int:
        """Number of configured proxies."""
        return len(self._proxies)

    @property
    def distinct_count(self) -> int:
        """Number of different proxy URLs."""
        return len(set(self._proxies))

    def pick(self, keyword_index: int) -> str | None:
        """Return the proxy for a keyword, or ``None`` when no proxy is set."""
        if not self._proxies:
            return None
        count = len(self._proxies)
        in_turn = [
            self._proxies[(keyword_index + self._shift + offset) % count]
            for offset in range(count)
        ]
        return next((p for p in in_turn if p not in self._failed), in_turn[0])

    def advance(self) -> None:
        """Move on to the next proxy for the following attempt."""
        self._shift += 1

    def mark_failed(self, proxy: str | None) -> None:
        """Remember that ``proxy`` failed so :meth:`pick` avoids it."""
        self._failed.add(proxy)

    def healthy_proxies(self) -> list[str]:
        """Return the proxies that did not fail (all of them if none is healthy)."""
        healthy = [p for p in self._proxies if p not in self._failed]
        return healthy or list(self._proxies)
