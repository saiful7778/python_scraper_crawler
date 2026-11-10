"""Pure retry timing: when another attempt is due and how long to wait."""

import email.utils
from collections.abc import Mapping

from .errors import RETRYABLE_ERRORS, FetchError
from .models import FetchPolicy


def parse_retry_after(value: str | None, now: float) -> float | None:
    """Seconds to wait according to a ``Retry-After`` header (``now`` is epoch time)."""
    if not value:
        return None
    value = value.strip()
    if value.isdigit():
        return float(value)
    try:
        moment = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    return max(0.0, moment.timestamp() - now)


def compute_retry_delay(
    policy: FetchPolicy, attempt: int, retry_after: float | None = None
) -> float | None:
    """Backoff before retry number ``attempt + 1``; ``None`` means give up.

    The delay grows linearly (``retry_delay * (attempt + 1)``), is raised to a
    server-requested ``Retry-After`` and never exceeds ``max_retry_delay``; a
    server that asks for longer than that is not waited for.
    """
    delay = policy.retry_delay * (attempt + 1)
    if policy.respect_retry_after and retry_after is not None:
        if retry_after > policy.max_retry_delay:
            return None
        delay = max(delay, retry_after)
    return min(delay, policy.max_retry_delay)


def retry_delay_after(
    policy: FetchPolicy,
    attempt: int,
    *,
    status: int | None = None,
    headers: Mapping[str, str] | None = None,
    error: FetchError | None = None,
    now: float = 0.0,
) -> float | None:
    """Seconds to wait before another attempt, or ``None`` when none is due."""
    if attempt >= policy.retries:
        return None
    if status is not None:
        if status not in policy.retry_statuses:
            return None
        retry_after = parse_retry_after((headers or {}).get("retry-after"), now)
        return compute_retry_delay(policy, attempt, retry_after)
    if error in RETRYABLE_ERRORS and policy.retry_network_errors:
        return compute_retry_delay(policy, attempt)
    return None
