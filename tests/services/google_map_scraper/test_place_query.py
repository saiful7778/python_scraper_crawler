import pytest
from pydantic import ValidationError

from app.services.google_map_scraper.place_query import PlaceQuery
from app.services.google_map_scraper.place_queue import run_id


def plans_of(**data) -> list[tuple[str, int, int]]:
    query = PlaceQuery.model_validate({"city": "Austin", "country": "us", **data})
    return [(p.keyword, p.depth, p.grid) for p in query.keyword_plans]


def test_plans_give_each_keyword_its_depth_and_grid():
    assert plans_of(
        plans=[
            {"keyword": "coffee shops", "depth": 6, "grid": 1},
            {"keyword": "pizza", "depth": 3, "grid": 2},
        ]
    ) == [("coffee shops", 6, 1), ("pizza", 3, 2)]


def test_plans_supply_the_keywords_and_are_read_by_one_based_index():
    query = PlaceQuery.model_validate(
        {
            "plans": [
                {"keyword": "coffee shops", "depth": 6},
                {"keyword": "pizza", "depth": 3, "grid": 2},
            ],
            "city": "Austin",
            "country": "us",
        }
    )
    assert query.keywords == ("coffee shops", "pizza")
    assert query.depth_of(1) == 6 and query.grid_of(1) == 1
    assert query.depth_of(2) == 3 and query.grid_of(2) == 2


def test_keywords_without_plans_inherit_the_run_level_depth_and_grid_one():
    assert plans_of(keywords=["cafes", "bakeries"]) == [
        ("cafes", 1, 1),
        ("bakeries", 1, 1),
    ]


def test_a_plan_with_no_depth_or_grid_gets_five_and_one():
    assert plans_of(plans=[{"keyword": "cafes"}]) == [("cafes", 5, 1)]


def test_a_run_level_grid_applies_to_every_derived_plan():
    assert plans_of(keywords=["cafes", "bakeries"], grid=2) == [
        ("cafes", 1, 2),
        ("bakeries", 1, 2),
    ]


def test_uses_grid_is_true_only_when_a_plan_tiles():
    whole_city = PlaceQuery.model_validate(
        {"keywords": ["cafes"], "city": "Austin", "country": "us"}
    )
    tiled = PlaceQuery.model_validate(
        {
            "plans": [
                {"keyword": "cafes", "grid": 1},
                {"keyword": "pizza", "grid": 2},
            ],
            "city": "Austin",
            "country": "us",
        }
    )
    assert whole_city.uses_grid is False
    assert tiled.uses_grid is True


def test_a_changed_plan_changes_the_run_id():
    first = PlaceQuery.model_validate(
        {"plans": [{"keyword": "cafes", "depth": 5}], "city": "Austin", "country": "us"}
    )
    deeper = PlaceQuery.model_validate(
        {"plans": [{"keyword": "cafes", "depth": 6}], "city": "Austin", "country": "us"}
    )
    assert run_id(first) != run_id(deeper)


def test_a_depth_out_of_range_names_the_depth_field():
    with pytest.raises(ValidationError) as error:
        PlaceQuery.model_validate(
            {
                "plans": [{"keyword": "cafes", "depth": 11}],
                "city": "Austin",
                "country": "us",
            }
        )
    assert error.value.errors()[0]["loc"][-1] == "depth"


def test_a_set_of_keywords_still_gets_one_plan_each():
    query = PlaceQuery.model_validate(
        {"keywords": {"cafes"}, "city": "Austin", "country": "us"}
    )
    assert [(p.keyword, p.depth, p.grid) for p in query.keyword_plans] == [
        ("cafes", 1, 1)
    ]


def test_keywords_and_plans_always_agree_in_length():
    with pytest.raises(ValidationError):
        PlaceQuery.model_validate(
            {
                "keywords": ["cafes"],
                "plans": [{"keyword": "cafes"}, {"keyword": "pizza"}],
                "city": "Austin",
                "country": "us",
            }
        )
