import pytest

from app.utils.validation import is_clean_text, read_bool, read_int


@pytest.mark.parametrize("value", ["abc", "café 東京", "line\nbreak", "tab\there", ""])
def test_is_clean_text_accepts_plain_text(value: str) -> None:
    assert is_clean_text(value) is True


@pytest.mark.parametrize(
    "value", ["nul\x00", "bell\x07", "\x0bvt", "del\x7f", "\ud800"]
)
def test_is_clean_text_rejects_control_and_unencodable_chars(value: str) -> None:
    assert is_clean_text(value) is False


@pytest.mark.parametrize("value", [None, 5, ["a"], b"bytes"])
def test_is_clean_text_rejects_non_strings(value: object) -> None:
    assert is_clean_text(value) is False


def test_read_bool_missing_or_none_is_false() -> None:
    assert read_bool({}, "flag") is False
    assert read_bool({"flag": None}, "flag") is False


def test_read_bool_returns_the_given_boolean() -> None:
    assert read_bool({"flag": True}, "flag") is True
    assert read_bool({"flag": False}, "flag") is False


@pytest.mark.parametrize("value", ["false", "true", 1, 0])
def test_read_bool_rejects_non_booleans(value: object) -> None:
    with pytest.raises(ValueError, match="invalid flag: true or false"):
        read_bool({"flag": value}, "flag")


def test_read_int_missing_or_none_uses_default() -> None:
    assert read_int({}, "zoom", 15, 1, 21, "zoom") == 15
    assert read_int({"zoom": None}, "zoom", 15, 1, 21, "zoom") == 15


@pytest.mark.parametrize("value", [1, 10, 21])
def test_read_int_accepts_bounds_inclusive(value: int) -> None:
    assert read_int({"zoom": value}, "zoom", 15, 1, 21, "zoom") == value


@pytest.mark.parametrize("value", [0, 22, -3])
def test_read_int_rejects_out_of_range(value: int) -> None:
    with pytest.raises(ValueError, match="invalid zoom: a whole number from 1 to 21"):
        read_int({"zoom": value}, "zoom", 15, 1, 21, "zoom")


@pytest.mark.parametrize("value", [True, "5", 5.0, [5]])
def test_read_int_rejects_booleans_strings_and_floats(value: object) -> None:
    with pytest.raises(ValueError, match="invalid zoom"):
        read_int({"zoom": value}, "zoom", 15, 1, 21, "zoom")
