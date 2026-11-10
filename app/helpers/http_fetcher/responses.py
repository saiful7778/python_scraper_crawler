"""Pure helpers for response status, headers and body decoding."""

from collections.abc import Iterable, Mapping


def is_success_status(status: int) -> bool:
    return 200 <= status < 300


def normalize_headers(pairs: Iterable[tuple[str, str]]) -> dict[str, str]:
    """Lower-case header names; repeated headers are joined with ``", "``."""
    merged: dict[str, str] = {}
    for name, value in pairs:
        key = name.lower()
        merged[key] = f"{merged[key]}, {value}" if key in merged else value
    return merged


def content_type_of(headers: Mapping[str, str]) -> str:
    """The bare media type of a response, lower-cased."""
    return headers.get("content-type", "").split(";", 1)[0].strip().lower()


def charset_of(headers: Mapping[str, str]) -> str | None:
    """The ``charset`` parameter of the Content-Type header, if any."""
    for part in headers.get("content-type", "").split(";")[1:]:
        name, _, value = part.partition("=")
        if name.strip().lower() == "charset":
            return value.strip().strip("\"'") or None
    return None


def decode_body(body: bytes, charset: str | None = None) -> str:
    """Decode with ``charset`` when it is known, else UTF-8; never raises."""
    for encoding in (charset, "utf-8"):
        if not encoding:
            continue
        try:
            return body.decode(encoding, "replace")
        except LookupError:
            continue
    return body.decode("utf-8", "replace")
