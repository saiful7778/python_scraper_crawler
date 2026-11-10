"""Decide which response bodies to download and what to keep of them."""

from collections.abc import Mapping

from .constants import BODYLESS_STATUSES
from .errors import FetchError
from .models import BodyVerdict, FetchPolicy
from .responses import content_type_of, is_success_status


def should_read_body(
    status: int, headers: Mapping[str, str], policy: FetchPolicy
) -> bool:
    """Only 2xx pages of an accepted content type are worth downloading."""
    return (
        is_success_status(status)
        and status not in BODYLESS_STATUSES
        and content_type_of(headers) in policy.allowed_content_types
    )


def evaluate_body(
    status: int,
    headers: Mapping[str, str],
    body: bytes,
    read_error: FetchError | None,
    policy: FetchPolicy,
) -> BodyVerdict:
    """Decide what to keep of a final response: wrong type, too big, or cut short."""
    if (
        is_success_status(status)
        and status not in BODYLESS_STATUSES
        and content_type_of(headers) not in policy.allowed_content_types
    ):
        return BodyVerdict(b"", FetchError.UNSUPPORTED_CONTENT_TYPE)
    if len(body) > policy.max_page_bytes:
        return BodyVerdict(
            body=body[: policy.max_page_bytes],
            error=FetchError.RESPONSE_TOO_LARGE,
        )
    return BodyVerdict(body, read_error)
