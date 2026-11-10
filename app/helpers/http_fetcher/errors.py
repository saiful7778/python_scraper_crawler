"""Failure vocabulary shared by every layer of the fetcher."""

from enum import StrEnum


class FetchError(StrEnum):
    """Why a fetch produced no (or only partial) content."""

    INVALID_URL = "invalid_url"
    NON_PUBLIC_HOST = "non_public_host"
    UNSUPPORTED_CONTENT_TYPE = "unsupported_content_type"
    RESPONSE_TOO_LARGE = "response_too_large"
    REDIRECT_LIMIT = "redirect_limit"
    REDIRECT_LOOP = "redirect_loop"
    REDIRECT_OUT_OF_SCOPE = "redirect_out_of_scope"
    UNSAFE_REDIRECT = "unsafe_redirect"
    INVALID_REDIRECT = "invalid_redirect"
    TIMEOUT = "timeout"
    NETWORK_ERROR = "network_error"


RETRYABLE_ERRORS = frozenset({FetchError.TIMEOUT, FetchError.NETWORK_ERROR})


class FetchCancelled(Exception):
    """Raised when the caller's cancel event is set during a fetch."""


class InvalidURL(ValueError):
    """The URL is malformed or not a plain http(s) URL."""


class TransportError(Exception):
    """A transport could not obtain any response."""

    def __init__(self, error: FetchError, detail: str = ""):
        super().__init__(detail or error.value)
        self.error = error


def classify_network_error(
    error: BaseException, deadline_expired: bool = False
) -> FetchError:
    """Map a low-level exception onto :class:`FetchError`."""
    if deadline_expired:
        return FetchError.TIMEOUT
    reason = getattr(error, "reason", error)
    if isinstance(error, TimeoutError) or isinstance(reason, TimeoutError):
        return FetchError.TIMEOUT
    return FetchError.NETWORK_ERROR
