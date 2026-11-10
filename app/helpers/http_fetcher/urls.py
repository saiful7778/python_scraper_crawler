"""URL preparation for fetches, built on :mod:`app.utils.url_utils`."""

import urllib.parse

from app.utils.url_utils import normalize_url, validate_url

from .errors import InvalidURL


def hostname_of(url: str) -> str:
    """The full lower-case host; ``www.`` is kept because DNS checks need it."""
    return (urllib.parse.urlsplit(url).hostname or "").lower()


def prepare_url(url: str) -> str:
    """Turn user input into a validated, normalized http(s) URL."""
    try:
        normalized = normalize_url(url)
        validate_url(normalized)
    except ValueError as error:
        raise InvalidURL(str(error)) from error
    return normalized
