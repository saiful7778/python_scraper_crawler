import re
from urllib.parse import urlsplit, urlunsplit

from app.helpers.http_fetcher.urls import hostname_of, prepare_url

TWO_PART_SUFFIXES = frozenset({"co.uk", "com.au", "co.jp", "com.br", "co.nz", "co.za"})

_SCHEME = re.compile(r"[a-z][a-z0-9+.-]*://", re.IGNORECASE)


def origin_of(website: str) -> str:
    raw = website.strip()
    parts = urlsplit(prepare_url(raw if _SCHEME.match(raw) else "https://" + raw))
    return f"{parts.scheme}://{parts.netloc}"


def registrable_domain(host: str) -> str:
    labels = host.lower().strip(".").split(".")
    if len(labels) <= 2:
        return ".".join(labels)
    keep = 3 if ".".join(labels[-2:]) in TWO_PART_SUFFIXES else 2
    return ".".join(labels[-keep:])


def is_in_scope(url: str, domain: str) -> bool:
    host = hostname_of(url)
    return host == domain or host.endswith("." + domain)


def normalize(url: str) -> str:
    parts = urlsplit(url)
    path = parts.path.rstrip("/") or "/"
    netloc = parts.netloc.lower()
    return urlunsplit((parts.scheme.lower(), netloc, path, parts.query, ""))
