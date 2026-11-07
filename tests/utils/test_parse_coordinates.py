import pytest

from app.utils.parse_coordinates import parse_coordinates


def test_parses_text_coordinates_to_floats() -> None:
    assert parse_coordinates("52.52", "13.405") == (52.52, 13.405)


def test_accepts_signed_and_exponent_text() -> None:
    assert parse_coordinates("+30.5", "-120") == (30.5, -120.0)
    assert parse_coordinates("1e1", "0") == (10.0, 0.0)


def test_accepts_range_boundaries() -> None:
    assert parse_coordinates(90, 180) == (90.0, 180.0)
    assert parse_coordinates(-90, -180) == (-90.0, -180.0)


@pytest.mark.parametrize(
    ("latitude", "longitude"),
    [
        (91, 0),
        (0, 181),
        (-90.5, 0),
        ("nan", 0),
        ("inf", 0),
        ("abc", 0),
        (None, 0),
        (True, 0),
        (0, False),
        ("", "1"),
    ],
)
def test_rejects_invalid_coordinates(latitude: object, longitude: object) -> None:
    with pytest.raises(ValueError, match="invalid geo coordinates"):
        parse_coordinates(latitude, longitude)
