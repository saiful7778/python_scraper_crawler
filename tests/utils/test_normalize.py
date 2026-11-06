import pytest

from app.utils.normalize import normalize_unicode_text


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("\u00a0", ""),
        ("a\u00a0b", "a b"),
        ("a\u202fb", "a b"),
        ("a\u2007b", "a b"),
        ("a\u2009b", "a b"),
        ("a\u200bb", "ab"),
        ("\ufeffabc", "abc"),
        ("a\u00a0\u202f\u2007\u2009\u200b\ufeffb", "a    b"),
    ],
)
def test_replaces_special_whitespace_and_zero_width_chars(raw: str, expected: str) -> None:
    assert normalize_unicode_text(raw) == expected


def test_strips_leading_and_trailing_whitespace() -> None:
    assert normalize_unicode_text("  hello  ") == "hello"


def test_strips_special_chars_at_edges() -> None:
    assert normalize_unicode_text("\u00a0\ufeff hello \u200b\u00a0") == "hello"


def test_keeps_regular_inner_whitespace_and_ascii() -> None:
    assert normalize_unicode_text("hello  world\tx\n") == "hello  world\tx"


def test_preserves_non_special_unicode() -> None:
    assert normalize_unicode_text("café 東京 🚀") == "café 東京 🚀"


def test_empty_string_returns_empty_string() -> None:
    assert normalize_unicode_text("") == ""


@pytest.mark.parametrize("value", [None, 0, 1.5, b"bytes", ["a"], {"k": "v"}, object()])
def test_non_string_input_returns_empty_string(value: object) -> None:
    assert normalize_unicode_text(value) == ""
