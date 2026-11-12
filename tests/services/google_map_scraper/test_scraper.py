import pytest

from app.services.google_map_scraper.place_query import PlaceQuery
from app.services.google_map_scraper.scraper import (
    BoundingBoxError,
    finalize_rows,
    keep_row,
    parse_bounding_box,
)


def _query(**overrides) -> PlaceQuery:
    data = {"keyword": "cafe", "city": "Austin", "country": "us", **overrides}
    return PlaceQuery.model_validate(data)


def _row(**overrides) -> dict:
    return {
        "title": "Cafe",
        "latitude": 30.27,
        "longitude": -97.74,
        "phone": "",
        "website": "",
        **overrides,
    }


def test_parse_bounding_box_returns_four_floats():
    assert parse_bounding_box("Austin", ["1", "2", "3", "4"]) == (1.0, 2.0, 3.0, 4.0)


def test_parse_bounding_box_raises_for_three_values():
    with pytest.raises(BoundingBoxError, match="has 3 values, expected 4"):
        parse_bounding_box("Austin", ["1", "2", "3"])


def test_keep_row_applies_the_phone_filter():
    query = _query(has_phone=True)
    assert keep_row(_row(phone="+1 512"), query)
    assert not keep_row(_row(phone=""), query)


def test_keep_row_applies_the_website_filter():
    query = _query(has_website=True)
    assert keep_row(_row(website="https://cafe.example"), query)
    assert not keep_row(_row(website=""), query)


@pytest.mark.parametrize("blank", ["   ", "\n\t"])
def test_keep_row_treats_whitespace_as_no_website_or_phone(blank):
    assert not keep_row(_row(website=blank), _query(has_website=True))
    assert not keep_row(_row(phone=blank), _query(has_phone=True))


def test_keep_row_keeps_everything_without_filters():
    assert keep_row(_row(), _query())


def test_finalize_rows_drops_rows_outside_the_bounding_box():
    inside = _row(title="inside")
    outside = _row(title="outside", latitude=10.0, longitude=10.0)
    bounding_box = (30.0, 30.5, -98.0, -97.5)
    kept = finalize_rows([inside, outside], _query(), bounding_box)
    assert [row["title"] for row in kept] == ["inside"]


def test_finalize_rows_applies_the_limit():
    rows = [_row(title=str(index)) for index in range(5)]
    kept = finalize_rows(rows, _query(limit=2), None)
    assert [row["title"] for row in kept] == ["0", "1"]
