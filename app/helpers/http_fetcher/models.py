"""Data types exchanged between the fetcher, its transport and its callers."""

from collections.abc import Mapping
from dataclasses import dataclass

from .constants import (
    DEFAULT_CONTENT_TYPES,
    RETRY_STATUSES,
    SITE_HARD,
    SITE_TIMEOUT,
    UA,
)
from .errors import FetchError
from .responses import (
    charset_of,
    decode_body,
    is_success_status,
)


@dataclass(frozen=True)
class FetchPolicy:
    """How a website request is retried, capped and accepted."""

    retries: int = 2  # extra attempts after a transient failure
    retry_delay: float = 1.0  # seconds; grows by one delay per attempt
    max_retry_delay: float = 30.0  # longer waits (also Retry-After) give up
    respect_retry_after: bool = True
    retry_statuses: frozenset[int] = RETRY_STATUSES
    retry_network_errors: bool = False  # also retry timeouts and resets
    max_page_bytes: int = 2_000_000
    allowed_content_types: tuple[str, ...] = DEFAULT_CONTENT_TYPES
    max_redirects: int = 5
    min_host_delay: float = 0.0  # seconds between two requests to the same host
    timeout: float = SITE_TIMEOUT
    hard_timeout: float = SITE_HARD
    user_agent: str = UA
    accept: str = "text/html"

    def __post_init__(self) -> None:
        non_negative = (
            "retries",
            "retry_delay",
            "max_retry_delay",
            "max_redirects",
            "min_host_delay",
        )
        for name in non_negative:
            if getattr(self, name) < 0:
                raise ValueError(f"{name} must not be negative")
        for name in ("max_page_bytes", "timeout", "hard_timeout"):
            if getattr(self, name) <= 0:
                raise ValueError(f"{name} must be positive")


@dataclass(frozen=True)
class FetchResult:
    """Outcome of one fetch, including redirects and failures.

    ``status`` is 0 when no HTTP response arrived.  ``error`` is ``None`` for
    plain HTTP outcomes (a 404 is a result, not an error).  A body can be
    present together with an error: a truncated or interrupted page.
    """

    url: str
    final_url: str
    status: int
    headers: Mapping[str, str]
    body: bytes
    redirect_chain: tuple[str, ...]
    error: FetchError | None = None
    detail: str = ""
    attempts: int = 0
    elapsed: float = 0.0

    @property
    def is_success(self) -> bool:
        """True for a 2xx response, whatever else went wrong afterwards."""
        return is_success_status(self.status)

    @property
    def text(self) -> str:
        """The body decoded with the declared charset (UTF-8 fallback)."""
        return decode_body(self.body, charset_of(self.headers))


@dataclass(frozen=True)
class RawResponse:
    """One HTTP exchange as a transport reports it (header names lower-case)."""

    status: int
    headers: Mapping[str, str]
    body: bytes = b""
    read_error: FetchError | None = None  # set when the body ended early


@dataclass(frozen=True)
class BodyRead:
    data: bytes
    error: FetchError | None = None


@dataclass(frozen=True)
class BodyVerdict:
    body: bytes
    error: FetchError | None = None


@dataclass(frozen=True)
class RedirectPlan:
    """Where a redirect leads, or why it must not be followed."""

    url: str | None = None
    error: FetchError | None = None


def build_result(
    *,
    trail: list[str] | tuple[str, ...],
    status: int = 0,
    headers: Mapping[str, str] | None = None,
    body: bytes = b"",
    error: FetchError | None = None,
    detail: str = "",
    attempts: int = 0,
    elapsed: float = 0.0,
) -> FetchResult:
    return FetchResult(
        url=trail[0],
        final_url=trail[-1],
        status=status,
        headers=dict(headers or {}),
        body=body,
        redirect_chain=tuple(trail[1:]),
        error=error,
        detail=detail,
        attempts=attempts,
        elapsed=elapsed,
    )
