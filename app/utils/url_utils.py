"""Small pure URL helpers."""

import logging
import urllib.parse
from collections.abc import Sequence

from app.utils.errors import describe

logger = logging.getLogger(__name__)


def encode_url_as_ascii(url: str) -> str:
    """Percent-encode non-ASCII path and query parts and drop the fragment.

    URLs that are already ASCII, or cannot be parsed, are returned unchanged.
    """
    try:
        parts = urllib.parse.urlsplit(url)
        if parts.path.isascii() and parts.query.isascii():
            return url
        return urllib.parse.urlunsplit(
            (
                parts.scheme,
                parts.netloc,
                urllib.parse.quote(parts.path, safe="/%:@&=+$,;~!*'()"),
                urllib.parse.quote(parts.query, safe="=&%+/?:@;,!*'()$"),
                "",
            )
        )
    except ValueError as error:
        logger.warning("could not encode URL %r as ASCII, using it as is: %s", url, describe(error))
        return url


def extract_site_host(url: str) -> str:
    """Return the lower-case host of ``url`` without a leading ``www.``."""
    try:
        host = (
            urllib.parse.urlsplit(url if "://" in url else "http://" + url).hostname
            or ""
        ).lower()
    except ValueError as error:
        logger.debug("no host in %r: %s", url, describe(error))
        return ""
    return host.removeprefix("www.")


JUNK_DOMAIN = (
    "example",
    "sentry",
    "wixpress.com",
    "domain.com",
    "yourdomain",
    "email.com",
)


def is_junk_domain(domain: str, junk_entries: Sequence[str] = JUNK_DOMAIN) -> bool:
    """Return True when ``domain`` matches a junk entry.

    An entry containing a dot matches the domain itself or any subdomain of it;
    an entry without a dot matches any single label of the domain.
    """
    labels = domain.split(".")
    return any(
        (
            (domain == junk or domain.endswith("." + junk))
            if "." in junk
            else junk in labels
        )
        for junk in junk_entries
    )
