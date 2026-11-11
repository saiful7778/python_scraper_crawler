import json
from collections.abc import Sequence
from typing import Any

import pytest

from app.services.google_map_scraper import extract_details
from app.services.google_map_scraper.extract_details import (
    ABOUT_PATH,
    ACTIONS_PATH,
    ADDRESS_PATH,
    FIELD_PATHS,
    HISTOGRAM_PATH,
    HOURS_PATH,
    IMAGE_URL_MAX,
    IMAGES_MAX,
    MENU_PATH,
    RESERVATIONS_PATH,
    REVIEWS_PATH,
    PanelRecord,
    Path,
    PlaceRecord,
    ReviewRecord,
    build_place_record,
    encode_plus_code,
    extract_attributes_and_cards,
    format_address,
    format_gallery,
    format_menu_link,
    format_sourced_links,
    format_weekly_hours,
    get_at_path,
    get_list_at_path,
    is_safe_public_url,
    parse_place_list,
    parse_place_panel,
)


def at(path: Path, value: Any) -> list[Any]:
    """Nest value at the given index path, padding the other slots with None."""
    node: Any = value
    for i in reversed(path):
        slot: list[Any] = [None] * (i + 1)
        slot[i] = node
        node = slot
    return node


def _merge(a: Any, b: Any) -> Any:
    if isinstance(a, list) and isinstance(b, list):
        return [
            _merge(a[i] if i < len(a) else None, b[i] if i < len(b) else None)
            for i in range(max(len(a), len(b)))
        ]
    return a if b is None else b


def merge(*nodes: Any) -> Any:
    result: Any = None
    for node in nodes:
        result = _merge(result, node)
    return result


def place(**fields: Any) -> Any:
    return merge(*(at(FIELD_PATHS[name], value) for name, value in fields.items()))


def day(name: str, number: int, spans: Sequence[str]) -> list[Any]:
    return [name, number, None, [[s] for s in spans]]


def option(name: str, on: bool, values: Sequence[str] = ()) -> Any:
    return merge(
        at((1,), name),
        at((2, 1, 0, 0), 1 if on else 0),
        at((2, 4, 1, 0, 0), [[None, None, v] for v in values]),
    )


def group(gid: str, name: str, options: list[Any]) -> Any:
    return merge(at((0,), gid), at((1,), name), at((2,), options))


def review(rating: int | str | bool, when: str, text: str) -> list[Any]:
    head: list[Any] = [
        None,
        at((6,), when),
        merge(at((0, 0), rating), at((15, 0, 0), text)),
    ]
    return [head]


def panel_body(entity: Any) -> str:
    return ")]}'\n" + json.dumps([None] * 6 + [entity])


class TestGetAtPath:
    def test_walks_nested_lists(self) -> None:
        node: list[Any] = [[1, [2, 3]]]
        assert get_at_path(node, (0, 1, 0)) == 2

    def test_empty_path_returns_node(self) -> None:
        node: list[int] = [1, 2]
        assert get_at_path(node, ()) is node

    def test_out_of_range_index_returns_none(self) -> None:
        node: list[int] = [1]
        assert get_at_path(node, (5,)) is None

    def test_none_along_the_way_returns_none(self) -> None:
        node: list[Any] = [None, [1]]
        assert get_at_path(node, (0, 0)) is None

    def test_non_list_intermediate_returns_none(self) -> None:
        assert get_at_path({"a": 1}, (0,)) is None
        assert get_at_path("abc", (0,)) is None


class TestGetListAtPath:
    def test_returns_list(self) -> None:
        node: list[list[int]] = [[1, 2]]
        result: list[int] = get_list_at_path(node, (0,))
        assert result == [1, 2]

    def test_non_list_value_returns_empty_list(self) -> None:
        node: list[list[str]] = [["x"]]
        result: list[Any] = get_list_at_path(node, (0, 0))
        assert result == []

    def test_missing_path_returns_empty_list(self) -> None:
        node: list[Any] = []
        result: list[Any] = get_list_at_path(node, (3,))
        assert result == []


class TestIsSafePublicUrl:
    @pytest.mark.parametrize(
        "url",
        ["https://example.com", "https://example.com/a?b=c", "https://sub.site.org/x"],
    )
    def test_accepts_https_urls(self, url: str) -> None:
        assert is_safe_public_url(url) is True

    def test_rejects_http_by_default(self) -> None:
        assert is_safe_public_url("http://example.com") is False

    def test_accepts_http_when_allowed(self) -> None:
        schemes: tuple[str, ...] = ("https", "http")
        assert is_safe_public_url("http://example.com", schemes) is True

    @pytest.mark.parametrize(
        "url",
        ["javascript:alert(1)", "ftp://example.com", "data:text/plain,hi"],
    )
    def test_rejects_other_schemes(self, url: str) -> None:
        schemes: tuple[str, ...] = ("https", "http")
        assert is_safe_public_url(url, schemes) is False

    def test_rejects_non_strings(self) -> None:
        assert is_safe_public_url(None) is False
        assert is_safe_public_url(["https://example.com"]) is False

    def test_rejects_missing_host(self) -> None:
        assert is_safe_public_url("https://") is False

    def test_rejects_credentials(self) -> None:
        assert is_safe_public_url("https://user:pass@example.com") is False
        assert is_safe_public_url("https://user@example.com") is False

    def test_rejects_whitespace_and_control_characters(self) -> None:
        assert is_safe_public_url("https://example.com/a b") is False
        assert is_safe_public_url("https://example.com/\n") is False
        assert is_safe_public_url("https://example.com/\x01") is False

    def test_rejects_overlong_urls(self) -> None:
        url: str = "https://example.com/" + "a" * IMAGE_URL_MAX
        assert len(url) > IMAGE_URL_MAX
        assert is_safe_public_url(url) is False

    def test_rejects_unparseable_urls(self) -> None:
        assert is_safe_public_url("https://[::1") is False


class TestFormatWeeklyHours:
    def full_week(self) -> list[list[Any]]:
        names: list[str] = [
            "Monday",
            "Tuesday",
            "Wednesday",
            "Thursday",
            "Friday",
            "Saturday",
            "Sunday",
        ]
        return [day(name, i + 1, ["9 AM–5 PM"]) for i, name in enumerate(names)]

    def test_returns_none_for_non_list(self) -> None:
        assert format_weekly_hours(None) is None
        assert format_weekly_hours("Monday") is None

    def test_returns_none_when_fewer_than_seven_days(self) -> None:
        days: list[list[Any]] = self.full_week()[:6]
        assert format_weekly_hours(days) is None

    def test_returns_none_when_days_repeat(self) -> None:
        days: list[list[Any]] = self.full_week()
        days[6] = day("Sunday", 1, [])
        assert format_weekly_hours(days) is None

    def test_keys_week_by_day_name(self) -> None:
        result = format_weekly_hours(self.full_week())
        assert result is not None
        assert list(result) == [
            "Monday",
            "Tuesday",
            "Wednesday",
            "Thursday",
            "Friday",
            "Saturday",
            "Sunday",
        ]
        assert result["Monday"] == ["9 AM–5 PM"]

    def test_orders_days_by_number(self) -> None:
        result = format_weekly_hours(list(reversed(self.full_week())))
        assert result is not None
        assert list(result) == [
            "Monday",
            "Tuesday",
            "Wednesday",
            "Thursday",
            "Friday",
            "Saturday",
            "Sunday",
        ]

    def test_keeps_non_ascii_and_normalizes_spans(self) -> None:
        days: list[list[Any]] = self.full_week()
        days[0] = day("Lundi", 1, ["9\u202f:00\u00a0–\u00a017:00", "\u200b", ""])
        result = format_weekly_hours(days)
        assert result is not None
        assert result["Monday"] == ["9 :00 – 17:00"]

    def test_skips_malformed_day_entries(self) -> None:
        days: list[Any] = self.full_week()
        days.append("not a day")
        days.append(["Short", 9, None, []])
        days.append(["Bad number", "1", None, []])
        days.append(["Bool number", True, None, []])
        result = format_weekly_hours(days)
        assert result is not None
        assert len(result) == 7


class TestFormatGallery:
    def test_returns_empty_for_no_photos(self) -> None:
        assert format_gallery({}) == []

    def test_collects_photos_from_all_layouts(self) -> None:
        entity: Any = merge(
            at(
                (171, 0),
                [
                    merge(
                        at((3, 0, 6, 0), "https://a.example/1.jpg"), at((2,), " Front ")
                    )
                ],
            ),
            at((37, 0), [at((6, 0), "https://b.example/2.jpg")]),
            at((72, 0), [at((6, 0), "https://c.example/3.jpg")]),
        )
        result = format_gallery(entity)
        assert result == [
            {"title": "Front", "image": "https://a.example/1.jpg"},
            {"title": "", "image": "https://b.example/2.jpg"},
            {"title": "", "image": "https://c.example/3.jpg"},
        ]

    def test_skips_duplicates_and_unsafe_urls(self) -> None:
        entity: Any = merge(
            at(
                (37, 0),
                [
                    at((6, 0), "https://a.example/1.jpg"),
                    at((6, 0), "https://a.example/1.jpg"),
                    at((6, 0), "http://insecure.example/2.jpg"),
                    at((6, 0), ["https://a.example/list"]),
                    None,
                ],
            ),
        )
        result = format_gallery(entity)
        assert result == [{"title": "", "image": "https://a.example/1.jpg"}]

    def test_caps_results_at_images_max(self) -> None:
        items: list[list[Any]] = [
            at((6, 0), f"https://img.example/{i}.jpg") for i in range(IMAGES_MAX + 5)
        ]
        result = format_gallery(at((37, 0), items))
        assert len(result) == IMAGES_MAX
        assert result[-1]["image"] == f"https://img.example/{IMAGES_MAX - 1}.jpg"

    def test_ignores_items_that_are_not_lists(self) -> None:
        assert format_gallery(at((37, 0), "not a list")) == []


class TestFormatAddress:
    def test_maps_address_parts(self) -> None:
        entity: list[Any] = at(
            ADDRESS_PATH,
            ["Downtown", "1 Main St", None, "Springfield", "12345", "IL", "US"],
        )
        result = format_address(entity)
        assert result == {
            "borough": "Downtown",
            "street": "1 Main St",
            "city": "Springfield",
            "postal_code": "12345",
            "state": "IL",
            "country": "US",
        }

    def test_missing_parts_become_empty_strings(self) -> None:
        entity: list[Any] = at(ADDRESS_PATH, ["", "1 Main St"])
        result = format_address(entity)
        assert result == {
            "borough": "",
            "street": "1 Main St",
            "city": "",
            "postal_code": "",
            "state": "",
            "country": "",
        }

    def test_normalizes_unicode_spaces(self) -> None:
        entity: list[Any] = at(ADDRESS_PATH, [None, "1\u00a0Main St"])
        result = format_address(entity)
        assert result is not None
        assert result["street"] == "1 Main St"

    def test_returns_none_when_no_parts(self) -> None:
        assert format_address({}) is None
        assert format_address(at(ADDRESS_PATH, ["", "", None])) is None

    def test_returns_none_for_non_list_parts(self) -> None:
        assert format_address(at(ADDRESS_PATH, "1 Main St")) is None


class TestExtractAttributesAndCards:
    def test_returns_empty_when_no_about(self) -> None:
        groups_json: str
        cards: str
        groups_json, cards = extract_attributes_and_cards({})
        assert (groups_json, cards) == ("", "")

    def test_builds_groups_with_options_and_values(self) -> None:
        entity: list[Any] = at(
            ABOUT_PATH,
            [
                group(
                    "accessibility",
                    "Accessibility",
                    [
                        option("Wheelchair accessible entrance", True),
                        option("Parking", False, ["Street"]),
                    ],
                ),
            ],
        )
        groups_json: str
        cards: str
        groups_json, cards = extract_attributes_and_cards(entity)
        result: list[dict[str, Any]] = json.loads(groups_json)
        assert result == [
            {
                "id": "accessibility",
                "name": "Accessibility",
                "options": [
                    {"name": "Wheelchair accessible entrance", "enabled": True},
                    {"name": "Parking", "enabled": False, "values": ["Street"]},
                ],
            }
        ]
        assert cards == ""

    def test_collects_credit_cards_from_payments_group(self) -> None:
        entity: list[Any] = at(
            ABOUT_PATH,
            [
                group(
                    "payments",
                    "Payments",
                    [
                        option("Credit cards", True, ["Visa", "Mastercard"]),
                    ],
                ),
                group(
                    "other",
                    "Other",
                    [
                        option("Credit cards", True, ["Visa"]),
                    ],
                ),
            ],
        )
        cards: str
        _, cards = extract_attributes_and_cards(entity)
        assert cards == "Visa, Mastercard"

    def test_duplicate_option_is_enabled_if_either_says_so(self) -> None:
        entity: list[Any] = at(
            ABOUT_PATH,
            [group("g", "G", [option("Wifi", False), option("Wifi", True, ["Free"])])],
        )
        groups_json: str
        groups_json, _ = extract_attributes_and_cards(entity)
        options: list[dict[str, Any]] = json.loads(groups_json)[0]["options"]
        assert options == [{"name": "Wifi", "enabled": True, "values": ["Free"]}]

    def test_skips_options_without_name_and_groups_without_content(self) -> None:
        entity: list[Any] = at(
            ABOUT_PATH,
            [
                group("", "", [option("", True)]),
                "not a group",
                group("g", "G", [option("Outdoor seating", True)]),
            ],
        )
        groups_json: str
        groups_json, _ = extract_attributes_and_cards(entity)
        result: list[dict[str, Any]] = json.loads(groups_json)
        assert result == [
            {
                "id": "g",
                "name": "G",
                "options": [{"name": "Outdoor seating", "enabled": True}],
            }
        ]


class TestFormatSourcedLinks:
    def test_returns_empty_for_non_list(self) -> None:
        assert format_sourced_links(None, (0,), (1,)) == []

    def test_builds_link_and_source_pairs(self) -> None:
        nodes: list[list[str]] = [
            ["https://a.example", "Site A"],
            ["http://b.example", "Site B"],
        ]
        result = format_sourced_links(nodes, (0,), (1,))
        assert result == [
            {"link": "https://a.example", "source": "Site A"},
            {"link": "http://b.example", "source": "Site B"},
        ]

    def test_skips_missing_source_unsafe_urls_and_duplicates(self) -> None:
        nodes: list[list[str]] = [
            ["https://a.example", ""],
            ["https://a.example", "Site A"],
            ["https://a.example", "Site A again"],
            ["javascript:alert(1)", "Bad"],
        ]
        result = format_sourced_links(nodes, (0,), (1,))
        assert result == [{"link": "https://a.example", "source": "Site A"}]

    def test_returns_empty_when_nothing_valid(self) -> None:
        nodes: list[list[str]] = [["ftp://x.example", "S"]]
        assert format_sourced_links(nodes, (0,), (1,)) == []


class TestFormatMenuLink:
    def test_returns_link_and_source(self) -> None:
        entity: list[Any] = at(MENU_PATH, ["https://menu.example", "Menu Site"])
        result: dict[str, str] = json.loads(format_menu_link(entity))
        assert result == {
            "link": "https://menu.example",
            "source": "Menu Site",
        }

    def test_returns_empty_for_unsafe_link(self) -> None:
        entity: list[Any] = at(MENU_PATH, ["javascript:void(0)", "Bad"])
        assert format_menu_link(entity) == ""

    def test_returns_empty_when_missing(self) -> None:
        assert format_menu_link({}) == ""


class TestEncodePlusCode:
    def test_matches_known_plus_codes(self) -> None:
        code: str = encode_plus_code(37.4220, -122.0841)
        assert code == "849VCWC8+R9"
        assert encode_plus_code(0, 0) == "6FG22222+22"

    def test_has_plus_sign_after_eighth_digit(self) -> None:
        code: str = encode_plus_code(48.8584, 2.2945)
        assert len(code) == 11
        assert code[8] == "+"

    def test_accepts_string_coordinates(self) -> None:
        assert encode_plus_code("37.4220", "-122.0841") == "849VCWC8+R9"

    @pytest.mark.parametrize(
        "lat, lon",
        [("x", 0), (None, 0), (0, "y"), (float("nan"), 0), (0, float("inf"))],
    )
    def test_returns_empty_for_invalid_coordinates(
        self, lat: str | float | None, lon: str | float | None
    ) -> None:
        assert encode_plus_code(lat, lon) == ""

    def test_clamps_out_of_range_latitude(self) -> None:
        assert encode_plus_code(120, 0) == encode_plus_code(90, 0)
        assert encode_plus_code(-120, 0) == encode_plus_code(-90, 0)

    def test_wraps_longitude(self) -> None:
        assert encode_plus_code(10, 190) == encode_plus_code(10, -170)


class TestParsePlacePanel:
    def test_returns_empty_for_invalid_json(self) -> None:
        result: PanelRecord = parse_place_panel(")]}'\n{not json")
        assert result == {}

    def test_returns_empty_when_entity_missing(self) -> None:
        assert parse_place_panel(")]}'\n[1, 2]") == {}
        assert parse_place_panel(json.dumps([None] * 6 + ["text"])) == {}

    def test_keeps_only_panel_fields(self) -> None:
        entity: Any = place(
            title="Cafe",
            review_count=120,
            review_rating=4.5,
            reviews_link="https://reviews.example",
        )
        result: PanelRecord = parse_place_panel(panel_body(entity))
        assert result == {
            "review_count": 120,
            "reviews_link": "https://reviews.example",
            "user_reviews": "",
        }
        assert "title" not in result
        assert "review_rating" not in result

    def test_accepts_body_without_prefix(self) -> None:
        entity: Any = place(review_count=3)
        result: PanelRecord = parse_place_panel(json.dumps([None] * 6 + [entity]))
        assert result == {"review_count": 3, "user_reviews": ""}

    def test_adds_reviews_per_rating_histogram(self) -> None:
        entity: Any = merge(place(review_count=10), at(HISTOGRAM_PATH, [1, 2, 3, 4, 0]))
        result: PanelRecord = parse_place_panel(panel_body(entity))
        assert "reviews_per_rating" in result
        histogram: dict[str, int] = json.loads(result["reviews_per_rating"])
        assert histogram == {
            "1": 1,
            "2": 2,
            "3": 3,
            "4": 4,
            "5": 0,
        }

    @pytest.mark.parametrize(
        "hist",
        [
            [1, 2, 3, 4],
            [1, 2, 3, 4, "5"],
            "12345",
            [1, 2, 3, 4, True],
        ],
    )
    def test_ignores_malformed_histogram(self, hist: list[Any] | str) -> None:
        entity: list[Any] = at(HISTOGRAM_PATH, hist)
        result: PanelRecord = parse_place_panel(panel_body(entity))
        assert "reviews_per_rating" not in result

    def test_parses_reviews(self) -> None:
        reviews: list[list[Any]] = [
            review(5, "a week ago", "Great"),
            review(2, "a year ago", "Meh"),
        ]
        entity: list[Any] = at(REVIEWS_PATH, reviews)
        result: PanelRecord = parse_place_panel(panel_body(entity))
        assert "user_reviews" in result
        parsed: list[ReviewRecord] = json.loads(result["user_reviews"])
        assert parsed == [
            {"Rating": 5, "When": "a week ago", "Description": "Great"},
            {"Rating": 2, "When": "a year ago", "Description": "Meh"},
        ]

    def test_limits_reviews(self) -> None:
        reviews: list[list[Any]] = [review(5, "now", f"Review {i}") for i in range(10)]
        entity: list[Any] = at(REVIEWS_PATH, reviews)
        result: PanelRecord = parse_place_panel(panel_body(entity), limit=3)
        assert "user_reviews" in result
        parsed: list[ReviewRecord] = json.loads(result["user_reviews"])
        assert [r["Description"] for r in parsed] == [
            "Review 0",
            "Review 1",
            "Review 2",
        ]

    def test_skips_reviews_without_integer_rating(self) -> None:
        reviews: list[list[Any]] = [
            review("5", "now", "string rating"),
            review(True, "now", "bool rating"),
            review(4, "now", "kept"),
        ]
        entity: list[Any] = at(REVIEWS_PATH, reviews)
        result: PanelRecord = parse_place_panel(panel_body(entity))
        assert "user_reviews" in result
        parsed: list[ReviewRecord] = json.loads(result["user_reviews"])
        assert parsed == [{"Rating": 4, "When": "now", "Description": "kept"}]

    def test_empty_reviews_value_when_none_parse(self) -> None:
        result: PanelRecord = parse_place_panel(panel_body(place(review_count=1)))
        assert result.get("user_reviews") == ""


class TestBuildPlaceRecord:
    def test_maps_scalar_fields(self) -> None:
        entity: Any = place(
            title="Cafe Nero",
            address="1 Main St",
            website="https://cafe.example",
            phone="+1 555 0100",
            review_rating=4.6,
            review_count=312,
            latitude=51.5,
            longitude=-0.12,
            place_id="ChIJ123",
            timezone="Europe/London",
            price_range="$$",
            description="A nice place",
        )
        record: PlaceRecord = build_place_record(entity)
        assert record.get("title") == "Cafe Nero"
        assert record.get("address") == "1 Main St"
        assert record.get("website") == "https://cafe.example"
        assert record.get("phone") == "+1 555 0100"
        assert record.get("review_rating") == 4.6
        assert record.get("review_count") == 312
        assert record.get("latitude") == 51.5
        assert record.get("longitude") == -0.12
        assert record.get("place_id") == "ChIJ123"
        assert record.get("timezone") == "Europe/London"
        assert record.get("price_range") == "$$"
        assert record.get("description") == "A nice place"

    def test_skips_empty_bool_and_non_scalar_values(self) -> None:
        entity: Any = merge(
            place(title="", website=True, phone=None), at(FIELD_PATHS["address"], ["x"])
        )
        record: PlaceRecord = build_place_record(entity)
        assert "title" not in record
        assert "website" not in record
        assert "phone" not in record
        assert "address" not in record

    def test_lowercases_and_dedupes_categories(self) -> None:
        entity: Any = merge(
            at(FIELD_PATHS["title"], "Shop"),
            at((13,), ["Coffee Shop", "", "Cafe", None, "coffee shop", "CAFE"]),
        )
        record: PlaceRecord = build_place_record(entity)
        assert record.get("categories") == ["coffee shop", "cafe"]

    def test_keeps_address_lines_as_list(self) -> None:
        entity: Any = at((2,), ["Line 1", "Line 2"])
        record: PlaceRecord = build_place_record(entity)
        assert record.get("address_lines") == ["Line 1", "Line 2"]

    def test_formats_opening_hours_when_full_week(self) -> None:
        names: list[str] = [
            "Monday",
            "Tuesday",
            "Wednesday",
            "Thursday",
            "Friday",
            "Saturday",
            "Sunday",
        ]
        days: list[list[Any]] = [
            day(n, i + 1, ["8 AM–6 PM"]) for i, n in enumerate(names)
        ]
        record: PlaceRecord = build_place_record(at(HOURS_PATH, days))
        assert "open_hours" in record
        hours = record.get("open_hours")
        assert hours is not None
        assert hours["Sunday"] == ["8 AM–6 PM"]

    def test_builds_cid_from_data_id(self) -> None:
        record: PlaceRecord = build_place_record(place(data_id="0x1:0xff"))
        assert record.get("cid") == "255"

    def test_no_cid_for_invalid_data_id(self) -> None:
        assert "cid" not in build_place_record(place(data_id="0x1:zz"))
        assert "cid" not in build_place_record(place(data_id="no-colon"))

    def test_includes_panel_fields_when_present(self) -> None:
        entity: Any = merge(
            at(ADDRESS_PATH, [None, "1 Main St", None, "Town"]),
            at(MENU_PATH, ["https://menu.example", "Menu"]),
        )
        record: PlaceRecord = build_place_record(entity)
        assert "complete_address" in record
        address = record["complete_address"]
        assert "menu" in record
        menu: dict[str, str] = json.loads(record["menu"])
        assert address["city"] == "Town"
        assert menu == {
            "link": "https://menu.example",
            "source": "Menu",
        }

    def test_builds_reservations_and_order_links(self) -> None:
        actions: list[Any] = [
            [1, None, [["https://book.example", "Booker"]]],
            [
                3,
                None,
                [
                    [["Delivery Co"], [None, None, ["https://order.example"]]],
                ],
            ],
        ]
        entity: Any = merge(
            at(RESERVATIONS_PATH, [["https://book.example", "Booker"]]),
            at(ACTIONS_PATH, actions),
        )
        record: PlaceRecord = build_place_record(entity)
        assert "reservations" in record
        reservations = record["reservations"]
        assert "order_online" in record
        order_online = record["order_online"]
        assert reservations == [{"link": "https://book.example", "source": "Booker"}]
        assert order_online == [
            {"link": "https://order.example", "source": "Delivery Co"}
        ]

    def test_builds_gallery_and_attributes(self) -> None:
        entity: Any = merge(
            at((37, 0), [at((6, 0), "https://img.example/1.jpg")]),
            at(
                ABOUT_PATH,
                [
                    group(
                        "payments", "Payments", [option("Credit cards", True, ["Visa"])]
                    )
                ],
            ),
        )
        record: PlaceRecord = build_place_record(entity)
        images = record.get("images")
        assert images is not None
        about_json = record.get("about")
        assert about_json is not None
        about: list[dict[str, Any]] = json.loads(about_json)
        assert images == [{"title": "", "image": "https://img.example/1.jpg"}]
        assert record.get("credit_cards_accepted") == "Visa"
        assert about[0]["id"] == "payments"

    def test_empty_entity_gives_empty_record(self) -> None:
        record: PlaceRecord = build_place_record([])
        assert record == {}


class TestParsePlaceList:
    def entry(self, entity: Any) -> list[Any]:
        return at((1,), entity)

    def body(self, entries: list[Any]) -> str:
        return ")]}'\n" + json.dumps(at((extract_details.ENTRIES_PATH,), entries))

    def test_returns_empty_for_invalid_json(self) -> None:
        rows: list[PlaceRecord] = parse_place_list(")]}'\n{oops")
        assert rows == []

    def test_returns_empty_when_results_missing(self) -> None:
        assert parse_place_list(json.dumps({"other": 1})) == []
        assert (
            parse_place_list(json.dumps(at((extract_details.ENTRIES_PATH,), "nope")))
            == []
        )

    def test_parses_each_entry_with_a_title(self) -> None:
        entries: list[list[Any]] = [
            self.entry(merge(place(title="First"), at((13,), ["Cafe"]))),
            self.entry(merge(place(title="Second"), at((13,), ["Bar"]))),
        ]
        rows: list[PlaceRecord] = parse_place_list(self.body(entries))
        assert [r.get("title") for r in rows] == ["First", "Second"]
        assert rows[0].get("categories") == ["cafe"]

    def test_skips_entries_without_title_or_entity(self) -> None:
        entries: list[list[Any]] = [
            self.entry(place(website="https://no-title.example")),
            self.entry(place(title="")),
            [None, "not an entity"],
            self.entry(place(title="Kept")),
        ]
        rows: list[PlaceRecord] = parse_place_list(self.body(entries))
        assert [r.get("title") for r in rows] == ["Kept"]

    def test_skips_entity_that_fails_to_build(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        real_build = extract_details.build_place_record

        def flaky(entity: Any) -> PlaceRecord:
            if get_at_path(entity, FIELD_PATHS["title"]) == "Broken":
                raise ValueError("odd entity")
            return real_build(entity)

        monkeypatch.setattr(extract_details, "build_place_record", flaky)
        entries: list[list[Any]] = [
            self.entry(place(title="Broken")),
            self.entry(place(title="Fine")),
        ]
        rows: list[PlaceRecord] = parse_place_list(self.body(entries))
        assert [r.get("title") for r in rows] == ["Fine"]

    def test_accepts_body_without_prefix(self) -> None:
        entries: list[list[Any]] = [self.entry(place(title="Plain"))]
        body: str = json.dumps(at((extract_details.ENTRIES_PATH,), entries))
        rows: list[PlaceRecord] = parse_place_list(body)
        assert [r.get("title") for r in rows] == ["Plain"]
