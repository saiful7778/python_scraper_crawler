import pytest

from app.services.google_map_scraper.google_map.parsing import (
    build_maps_search_url,
    feed_is_exhausted,
    parse_place_href,
)

PLACE_HREF = (
    "https://www.google.com/maps/place/Coffee+House+%26+Co/"
    "data=!4m7!3m6!1s0x89c25:0x123abc!8m2!3d40.7128!4d-74.0060!16s!19sChIJabc123"
    "?authuser=0"
)


def test_search_url_quotes_query_and_defaults_language():
    assert (
        build_maps_search_url("coffee shops")
        == "https://www.google.com/maps/search/coffee%20shops?hl=en"
    )


def test_search_url_encodes_separators_in_query():
    url = build_maps_search_url("a/b & c")

    assert "/search/a%2Fb%20%26%20c?" in url


def test_search_url_includes_viewport_when_lat_and_lon_given():
    assert (
        build_maps_search_url("cafe", lat=40.7, lon=-74.0)
        == "https://www.google.com/maps/search/cafe/@40.7,-74.0,15z?hl=en"
    )


def test_search_url_uses_given_zoom_in_viewport():
    assert "/@40.7,-74.0,12z" in build_maps_search_url(
        "cafe", lat=40.7, lon=-74.0, zoom=12
    )


def test_search_url_ignores_zoom_without_both_coordinates():
    assert build_maps_search_url("cafe", lat=40.7, zoom=12) == (
        "https://www.google.com/maps/search/cafe?hl=en"
    )


def test_search_url_adds_country_and_language():
    assert (
        build_maps_search_url("cafe", lang="de", country="de")
        == "https://www.google.com/maps/search/cafe?hl=de&gl=de"
    )


@pytest.mark.parametrize(
    "feed_state",
    [
        None,
        {"cls": False, "text": ""},
        {"cls": False, "text": "Loading more results"},
    ],
)
def test_feed_not_exhausted(feed_state):
    assert feed_is_exhausted(feed_state) is False


@pytest.mark.parametrize(
    "feed_state",
    [
        {"cls": True, "text": ""},
        {"cls": False, "text": "Results\nYou've reached the end of the list"},
        {"cls": False, "text": "Vous êtes arrivé à la fin de la liste"},
        {"cls": False, "text": "Has llegado al final de la lista"},
    ],
)
def test_feed_exhausted_by_class_or_locale_text(feed_state):
    assert feed_is_exhausted(feed_state) is True


def test_feed_exhausted_tolerates_missing_text():
    assert feed_is_exhausted({"cls": False}) is False  # type: ignore[typeddict-item]


def test_parse_place_href_extracts_all_fields():
    assert parse_place_href(PLACE_HREF) == {
        "link": PLACE_HREF,
        "title": "Coffee House & Co",
        "data_id": "0x89c25:0x123abc",
        "latitude": "40.7128",
        "longitude": "-74.0060",
        "place_id": "ChIJabc123",
    }


def test_parse_place_href_prefers_label_over_path_name():
    place = parse_place_href(PLACE_HREF, label="Labelled cafe")

    assert place["title"] == "Labelled cafe"


def test_parse_place_href_uses_path_name_when_label_is_empty():
    place = parse_place_href(PLACE_HREF, label="")

    assert place["title"] == "Coffee House & Co"


def test_parse_place_href_without_place_path_has_no_title():
    href = "https://www.google.com/search?q=cafe"

    place = parse_place_href(href)

    assert place == {
        "link": href,
        "title": "",
        "data_id": "",
        "latitude": "",
        "longitude": "",
        "place_id": "",
    }


def test_parse_place_href_without_place_path_falls_back_to_label():
    place = parse_place_href("https://www.google.com/search?q=cafe", "Cafe")

    assert place["title"] == "Cafe"


def test_parse_place_href_with_only_data_id_leaves_other_fields_empty():
    place = parse_place_href(
        "https://www.google.com/maps/place/Bar/data=!1s0x3:0x4"
    )

    assert place["title"] == "Bar"
    assert place["data_id"] == "0x3:0x4"
    assert place["latitude"] == ""
    assert place["longitude"] == ""
    assert place["place_id"] == ""
