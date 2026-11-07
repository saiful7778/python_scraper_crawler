"""Classify error messages by what caused them."""

import re

_PROXY_FAILURE = re.compile(r"ERR_(PROXY|TUNNEL)|proxy", re.IGNORECASE)
_TIMEOUT_FAILURE = re.compile(r"timed? ?out|ERR_TIMED_OUT", re.IGNORECASE)


def is_proxy_failure(message: str) -> bool:
    """Return True when the message says the proxy itself failed."""
    return bool(_PROXY_FAILURE.search(message))


def is_timeout_failure(message: str) -> bool:
    """Return True when the message says the page or connection timed out."""
    return bool(_TIMEOUT_FAILURE.search(message))
