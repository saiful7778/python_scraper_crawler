"""Checks for untrusted input values read from a request body."""

import re
from collections.abc import Mapping
from typing import Any

CONTROL = re.compile(
    r"[\x00-\x08\x0b-\x1f\x7f]"
)  # NUL breaks a stored row; the rest never belongs in a search


def is_clean_text(value: Any) -> bool:
    """Return True for a string without control characters that encodes as UTF-8."""
    if not isinstance(value, str) or CONTROL.search(value):
        return False
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def _require_object(body: Any) -> None:
    if not isinstance(body, Mapping):
        raise ValueError(  # noqa: TRY004
            f"the request body must be a JSON object, not {type(body).__name__}"
        )


def read_bool(body: Mapping[str, Any], key: str) -> bool:
    """Read an optional boolean; a missing key is False and ``"false"`` is rejected."""
    _require_object(body)
    value = body.get(key)
    if value is None:
        return False
    if not isinstance(value, bool):
        raise ValueError(f"invalid {key}: true or false")  # noqa: TRY004
    return value


def read_int(
    body: Mapping[str, Any], key: str, default: int, low: int, high: int, label: str
) -> int:
    """Read an optional whole number within ``[low, high]`` (booleans are rejected)."""
    _require_object(body)
    value = body.get(key, default)
    if value is None:
        value = default
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not low <= value <= high
    ):
        raise ValueError(f"invalid {label}: a whole number from {low} to {high}")
    return value
