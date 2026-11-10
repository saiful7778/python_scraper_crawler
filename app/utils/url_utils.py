"""Small pure URL helpers."""

import ipaddress
import re
import urllib.parse
from collections.abc import Sequence

from app.utils.errors import describe
from app.utils.logger import get_logger

logger = get_logger(__name__)

_SCHEME_PREFIX = re.compile(r"[a-z][a-z0-9+.-]*://", re.IGNORECASE)
_HTTP_PREFIX = re.compile(r"^(https?):/{0,3}", re.IGNORECASE)
_OTHER_SCHEME = re.compile(r"^[a-z][a-z0-9+.-]*://", re.IGNORECASE)

WEB_SCHEMES = frozenset({"http", "https"})
DEFAULT_PORTS = {"http": 80, "https": 443}

JUNK_DOMAIN = (
    "example",
    "sentry",
    "wixpress.com",
    "domain.com",
    "yourdomain",
    "email.com",
)


def _ensure_scheme(url: str) -> str:
    """Prefix ``http://`` unless ``url`` starts with a scheme."""
    return url if _SCHEME_PREFIX.match(url) else "http://" + url


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
        host = urllib.parse.urlsplit(_ensure_scheme(url)).hostname
    except ValueError as error:
        logger.debug("no host in %r: %s", url, describe(error))
        return ""
    return (host or "").removeprefix("www.")


def normalize_url(url: str) -> str:
    """Return ``url`` in canonical ASCII form, without a fragment.

    Lower-cases the scheme and host, drops a default port (``:80`` on http,
    ``:443`` on https), keeps IPv6 hosts in brackets and an empty path becomes
    ``/``.  A missing scheme defaults to ``http://``, so ``mailto:x`` becomes an
    http URL; check the scheme of the raw input first when that matters.
    Raises ``ValueError`` for unparsable URLs.
    """
    parts = urllib.parse.urlsplit(encode_url_as_ascii(_ensure_scheme(url.strip())))
    if not parts.netloc:
        return urllib.parse.urlunsplit(parts._replace(fragment=""))
    scheme = parts.scheme.lower()
    host = (parts.hostname or "").lower()
    if ":" in host:
        host = f"[{host}]"
    port = parts.port
    if port is not None and port == DEFAULT_PORTS.get(scheme):
        port = None
    userinfo = parts.netloc.rpartition("@")[0]
    netloc = f"{userinfo}@" if userinfo else ""
    netloc += host + (f":{port}" if port is not None else "")
    return urllib.parse.urlunsplit((scheme, netloc, parts.path or "/", parts.query, ""))


def validate_url(url: str) -> None:
    """Raise ``ValueError`` unless ``url`` is a plain http(s) URL with a host and no credentials."""
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in WEB_SCHEMES or not parts.hostname:
        raise ValueError(f"not a web URL: {url!r}")
    if parts.username is not None or parts.password is not None:
        raise ValueError(f"credentials in URL: {url!r}")


def website_candidates(raw: str) -> list[str]:
    """Return the URLs to try for a website as written in a listing, most likely first.

    Whitespace and quotes are dropped, a missing scheme is treated as https, and the
    host is lower-cased and IDNA-encoded. The host as given comes first, then its
    ``www.`` twin (or without ``www.``), and each host is tried over https before http.
    Raises ``ValueError`` when there is no host or the scheme is not http(s).
    """
    text = re.sub(r"\s+", "", raw.strip().strip("\"'<>"))
    if not text:
        raise ValueError("empty website")
    match = _HTTP_PREFIX.match(text)
    if match:
        rest = text[match.end() :]
    elif _OTHER_SCHEME.match(text):
        raise ValueError(f"not an http(s) website: {raw!r}")
    else:
        rest = text.lstrip("/")
    try:
        parts = urllib.parse.urlsplit("//" + rest)
        port = parts.port
        host = (parts.hostname or "").rstrip(".")
        host = host.encode("idna").decode("ascii") if host else ""
    except (ValueError, UnicodeError) as error:
        raise ValueError(f"bad website {raw!r}: {describe(error)}") from error
    if not host:
        raise ValueError(f"no host in website {raw!r}")
    if parts.username is not None or parts.password is not None:
        raise ValueError(f"credentials in website {raw!r}")
    if _is_ip(host) or "." not in host:
        hosts = [host]
    elif host.startswith("www."):
        hosts = [host, host.removeprefix("www.")]
    else:
        hosts = [host, "www." + host]
    path = parts.path or "/"
    candidates = []
    for name in hosts:
        if ":" in name:
            name = f"[{name}]"
        netloc = name if port is None else f"{name}:{port}"
        for scheme in ("https", "http"):
            candidates.append(
                urllib.parse.urlunsplit((scheme, netloc, path, parts.query, ""))
            )
    return candidates


def _is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return False
    return True


def same_scope(url: str, scope: str, allow_subdomains: bool = False) -> bool:
    """Return True when ``url`` is on the site of ``scope``.

    Hosts are compared without ``www.``; with ``allow_subdomains`` a subdomain
    of the scope host also counts.
    """
    host = extract_site_host(url)
    site = extract_site_host(scope)
    if not host or not site:
        return False
    return host == site or (allow_subdomains and host.endswith("." + site))


def _matches_junk(domain: str, junk: str) -> bool:
    if "." in junk:
        return domain == junk or domain.endswith("." + junk)
    return junk in domain.split(".")


def is_junk_domain(domain: str, junk_entries: Sequence[str] = JUNK_DOMAIN) -> bool:
    """Return True when ``domain`` matches a junk entry.

    An entry containing a dot matches the domain itself or any subdomain of it;
    an entry without a dot matches any single label of the domain.
    """
    return any(_matches_junk(domain, junk) for junk in junk_entries)
