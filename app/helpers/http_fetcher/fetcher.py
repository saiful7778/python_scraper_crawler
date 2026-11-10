"""Orchestration: retries, redirects and deadlines around one transport."""

import threading
from collections.abc import Callable
from dataclasses import dataclass, replace

from app.utils.errors import describe
from app.utils.logger import get_logger
from app.utils.network_guard import resolves_only_to_public_addresses

from .body import evaluate_body
from .constants import REDIRECT_STATUSES
from .errors import FetchCancelled, FetchError, TransportError
from .hard_stop import FetchWindow
from .models import (
    FetchPolicy,
    FetchResult,
    RawResponse,
    build_result,
)
from .pacing import HostPacer, RateLimit
from .redirects import plan_redirect
from .responses import normalize_headers
from .retry import retry_delay_after
from .timing import (
    SYSTEM_CLOCK,
    Clock,
    Deadline,
    interruptible_sleep,
    raise_if_cancelled,
)
from .transport import UrllibTransport
from .urls import hostname_of, prepare_url

logger = get_logger(__name__)


@dataclass(frozen=True)
class _Exchange:
    """Result of one URL after all its retries."""

    raw: RawResponse | None = None
    error: FetchError | None = None
    detail: str = ""
    attempts: int = 0


class WebPageFetcher:
    """Downloads the HTML of public web pages."""

    def __init__(
        self,
        policy: FetchPolicy | None = None,
        *,
        rate_limit: RateLimit | None = None,
        host_checker: Callable[[str], bool] = resolves_only_to_public_addresses,
        clock: Clock = SYSTEM_CLOCK,
    ):
        """Wire the collaborators; every one of them can be replaced in tests."""
        self._policy = policy or FetchPolicy()
        self._clock = clock
        self._transport = UrllibTransport()
        self._rate_limit = rate_limit or HostPacer(self._policy.min_host_delay, clock)
        self._host_checker = host_checker

    def fetch(
        self,
        url: str,
        timeout: float | None = None,
        scope: str | None = None,
        allow_subdomains: bool = False,
        cancel_event: threading.Event | None = None,
    ) -> str:
        """Return the page text, or ``""`` when it cannot be fetched.

        A missing scheme defaults to ``http://``.  Non-public hosts, errors,
        non-2xx answers, content types other than the allowed ones, and
        redirects that leave ``scope`` (when given) all yield an empty page; at
        most ``max_page_bytes`` are read.  Raises :class:`FetchCancelled` when
        ``cancel_event`` is set.
        """
        result = self.fetch_result(
            url,
            scope=scope,
            allow_subdomains=allow_subdomains,
            cancel_event=cancel_event,
            timeout=timeout,
        )
        return result.text if result.is_success else ""

    def fetch_result(
        self,
        url: str,
        *,
        scope: str | None = None,
        allow_subdomains: bool = False,
        cancel_event: threading.Event | None = None,
        timeout: float | None = None,
    ) -> FetchResult:
        """Fetch ``url``, following vetted redirects, and report what happened.

        Every failure mode is returned as :attr:`FetchResult.error`; only a set
        ``cancel_event`` raises (:class:`FetchCancelled`).
        """
        raise_if_cancelled(cancel_event)
        policy = (
            self._policy if timeout is None else replace(self._policy, timeout=timeout)
        )
        started = self._clock.monotonic()
        try:
            start = prepare_url(url)
        except ValueError as error:
            return build_result(
                trail=(url,), error=FetchError.INVALID_URL, detail=str(error)
            )

        trail = [start]
        attempts = 0

        def done(**fields) -> FetchResult:
            return build_result(
                trail=trail,
                attempts=attempts,
                elapsed=self._clock.monotonic() - started,
                **fields,
            )

        deadline = Deadline.after(policy.hard_timeout, self._clock)
        with FetchWindow(deadline):
            while True:
                current = trail[-1]
                if deadline.expired:
                    return done(error=FetchError.TIMEOUT, detail="hard time limit")
                if not self._is_public(current):
                    logger.debug(
                        "website %s skipped: it does not resolve to public addresses only",
                        hostname_of(current),
                    )
                    first_hop = len(trail) == 1
                    return done(
                        error=(
                            FetchError.NON_PUBLIC_HOST
                            if first_hop
                            else FetchError.UNSAFE_REDIRECT
                        ),
                        detail=f"{hostname_of(current)} is not public",
                    )
                exchange = self._exchange(current, policy, deadline, cancel_event)
                attempts += exchange.attempts
                raw = exchange.raw
                if raw is None:
                    return done(error=exchange.error, detail=exchange.detail)
                if raw.status in REDIRECT_STATUSES:
                    plan = plan_redirect(
                        trail,
                        raw.headers,
                        policy,
                        scope=scope,
                        allow_subdomains=allow_subdomains,
                    )
                    if plan.url is None:
                        return done(
                            status=raw.status, headers=raw.headers, error=plan.error
                        )
                    trail.append(plan.url)
                    continue
                verdict = evaluate_body(
                    raw.status, raw.headers, raw.body, raw.read_error, policy
                )
                return done(
                    status=raw.status,
                    headers=raw.headers,
                    body=verdict.body,
                    error=verdict.error,
                )

    def _is_public(self, url: str) -> bool:
        try:
            return bool(self._host_checker(hostname_of(url)))
        except Exception:  # noqa: BLE001 - an unresolvable host is not public
            return False

    def _exchange(
        self,
        url: str,
        policy: FetchPolicy,
        deadline: Deadline,
        cancel_event: threading.Event | None,
    ) -> _Exchange:
        """Send one request, retrying transient failures with a growing delay."""
        host = hostname_of(url)
        for attempt in range(policy.retries + 1):
            raise_if_cancelled(cancel_event)
            self._rate_limit.wait(host, cancel_event)
            raw, error, detail = self._send_once(url, policy, cancel_event)
            delay = retry_delay_after(
                policy,
                attempt,
                status=raw.status if raw else None,
                headers=raw.headers if raw else None,
                error=error,
                now=self._clock.time(),
            )
            if delay is None or delay >= deadline.remaining():
                return _Exchange(raw, error, detail, attempt + 1)
            logger.debug(
                "website %s answered %s, retrying in %.1fs",
                host,
                raw.status if raw else error,
                delay,
            )
            interruptible_sleep(delay, cancel_event, self._clock)
        raise RuntimeError("unreachable")

    def _send_once(
        self,
        url: str,
        policy: FetchPolicy,
        cancel_event: threading.Event | None,
    ) -> tuple[RawResponse | None, FetchError | None, str]:
        try:
            raw = self._transport.send(url, policy, cancel_event)
        except FetchCancelled:
            raise
        except TransportError as error:
            logger.debug("website %s failed: %s", hostname_of(url), error)
            return None, error.error, str(error)
        except Exception as error:  # noqa: BLE001 - a failed fetch is a result
            logger.debug(
                "website %s could not be fetched: %s", hostname_of(url), describe(error)
            )
            return None, FetchError.NETWORK_ERROR, describe(error)
        return replace(raw, headers=normalize_headers(raw.headers.items())), None, ""
