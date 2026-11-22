import pytest
from pydantic import ValidationError

from app.services.serper.maps import serper_map_plan as plan_module
from app.services.serper.maps.serper_map_config import SerperMapConfig
from app.services.serper.maps.serper_map_errors import SerperGeocodeError
from app.services.serper.maps.serper_map_plan import build_plan


def _config(**overrides):
    values = {
        "keywords": ("plumbers",),
        "city": "Austin",
        "state": "TX",
        "country": "US",
    }
    values.update(overrides)
    return SerperMapConfig(**values)


def _plans_config(plans, **overrides):
    values = {"plans": plans, "city": "Austin", "state": "TX", "country": "US"}
    values.update(overrides)
    return SerperMapConfig(**values)


def test_defaults_are_one_page_one_tile_and_five_parallel():
    config = _config()
    plan = config.plans[0]
    assert (plan.depth, plan.grid, config.parallel) == (1, 1, 5)
    assert config.place == "Austin, TX, US"
    assert config.name == "plumbers @ Austin, TX, US"


def test_every_keyword_has_its_own_depth_and_grid():
    config = _plans_config(
        [
            {"keyword": "plumber", "depth": 2, "grid": 4},
            {"keyword": "hvac", "depth": 4, "grid": 2},
        ]
    )
    assert config.keywords == ("plumber", "hvac")
    assert [(plan.depth, plan.grid) for plan in config.plans] == [(2, 4), (4, 2)]


def test_a_keyword_without_its_own_values_gets_one_page_one_tile():
    config = _plans_config([{"keyword": "plumber"}, {"keyword": "hvac", "depth": 4}])
    assert [(plan.depth, plan.grid) for plan in config.plans] == [(1, 1), (4, 1)]


def test_country_is_lowercased_and_depth_and_grid_are_bounded():
    assert _config(country="US").country == "us"
    with pytest.raises(ValidationError):
        _plans_config([{"keyword": "plumber", "depth": 0}])
    with pytest.raises(ValidationError):
        _plans_config([{"keyword": "plumber", "grid": 11}])
    with pytest.raises(ValidationError):
        _config(parallel=21)


def test_the_config_key_ignores_parallel_but_not_the_search():
    plans = [{"keyword": "plumber", "depth": 2, "grid": 3}]
    base = _plans_config(plans)
    assert base.config_key == _plans_config(plans, parallel=9).config_key
    assert (
        base.config_key
        != _plans_config([{"keyword": "plumber", "depth": 2, "grid": 4}]).config_key
    )
    assert (
        base.config_key
        != _plans_config([{"keyword": "plumber", "depth": 3, "grid": 3}]).config_key
    )
    assert base.config_key != _plans_config(plans, has_phone=True).config_key


def test_two_keywords_with_different_depths_are_a_different_search_than_one():
    both = _plans_config(
        [
            {"keyword": "plumber", "depth": 2, "grid": 4},
            {"keyword": "hvac", "depth": 4, "grid": 2},
        ]
    )
    swapped = _plans_config(
        [
            {"keyword": "plumber", "depth": 4, "grid": 4},
            {"keyword": "hvac", "depth": 2, "grid": 2},
        ]
    )
    assert both.config_key != swapped.config_key


def test_plans_and_keywords_must_name_the_same_keywords():
    with pytest.raises(ValidationError):
        _plans_config([{"keyword": "plumber"}], keywords=("hvac",))


def test_a_flat_depth_beside_plans_is_refused_rather_than_dropped():
    with pytest.raises(ValidationError):
        _plans_config([{"keyword": "plumber", "depth": 2}], depth=5)
    with pytest.raises(ValidationError):
        _plans_config([{"keyword": "plumber", "grid": 2}], grid=5)


def test_a_config_with_no_keyword_is_refused():
    with pytest.raises(ValidationError):
        SerperMapConfig(city="Austin", state="TX", country="US")
    with pytest.raises(ValidationError):
        _plans_config([])


def test_the_place_leaves_out_a_missing_state():
    assert _config(state=None).place == "Austin, US"


async def test_a_whole_city_is_one_tile_with_every_page(monkeypatch):
    monkeypatch.setattr(
        plan_module, "get_geocode", lambda *a, **k: ((30.26, -97.74), None)
    )
    built = await build_plan(_config(depth=3))
    assert built.tile_count == 1
    assert [page.page for page in built.pages] == [1, 2, 3]
    assert {(page.lat, page.lng, page.zoom) for page in built.pages} == {
        (30.26, -97.74, 15)
    }
    assert built.estimated_credits == 9


async def test_a_grid_becomes_n_by_n_tiles_and_every_page_per_tile(monkeypatch):
    box = [str(v) for v in (30.09, 30.51, -97.93, -97.56)]
    monkeypatch.setattr(
        plan_module, "get_geocode", lambda *a, **k: ((30.26, -97.74), box)
    )
    built = await build_plan(_config(depth=2, grid=3, keywords=("plumbers", "pizza")))
    assert built.tile_count == 18
    assert len(built.pages) == 2 * 9 * 2
    assert built.pages[0].ll.startswith("@")
    assert {(page.grid_x, page.grid_y) for page in built.pages} == {
        (x, y) for x in range(3) for y in range(3)
    }
    assert {page.keyword for page in built.pages} == {"plumbers", "pizza"}


async def test_each_keyword_gets_every_tile(monkeypatch):
    box = [str(v) for v in (30.09, 30.51, -97.93, -97.56)]
    monkeypatch.setattr(
        plan_module, "get_geocode", lambda *a, **k: ((30.26, -97.74), box)
    )
    built = await build_plan(_config(depth=1, grid=2, keywords=("a", "b")))
    for keyword in ("a", "b"):
        cells = {(p.grid_x, p.grid_y) for p in built.pages if p.keyword == keyword}
        assert cells == {(0, 0), (0, 1), (1, 0), (1, 1)}


async def test_a_tile_covers_all_its_pages_before_the_next_tile(monkeypatch):
    box = [str(v) for v in (30.09, 30.51, -97.93, -97.56)]
    monkeypatch.setattr(
        plan_module, "get_geocode", lambda *a, **k: ((30.26, -97.74), box)
    )
    built = await build_plan(_config(depth=2, grid=2))
    assert [(p.grid_x, p.grid_y, p.page) for p in built.pages] == [
        (0, 0, 1),
        (0, 0, 2),
        (1, 0, 1),
        (1, 0, 2),
        (0, 1, 1),
        (0, 1, 2),
        (1, 1, 1),
        (1, 1, 2),
    ]


async def test_each_keyword_is_tiled_with_its_own_grid(monkeypatch):
    box = [str(v) for v in (30.09, 30.51, -97.93, -97.56)]
    monkeypatch.setattr(
        plan_module, "get_geocode", lambda *a, **k: ((30.26, -97.74), box)
    )
    built = await build_plan(
        _plans_config(
            [
                {"keyword": "plumber", "depth": 2, "grid": 4},
                {"keyword": "hvac", "depth": 4, "grid": 2},
            ]
        )
    )
    plumber = [page for page in built.pages if page.keyword == "plumber"]
    hvac = [page for page in built.pages if page.keyword == "hvac"]
    assert len(plumber) == 4 * 4 * 2
    assert len(hvac) == 2 * 2 * 4
    assert len({(page.grid_x, page.grid_y) for page in plumber}) == 16
    assert len({(page.grid_x, page.grid_y) for page in hvac}) == 4
    assert built.tile_count == 16 + 4
    assert built.page_count == 48
    assert built.estimated_credits == 144
    assert built.bounding_box == (30.09, 30.51, -97.93, -97.56)


async def test_a_whole_city_keyword_keeps_the_city_zoom_beside_a_grid(monkeypatch):
    box = [str(v) for v in (30.09, 30.51, -97.93, -97.56)]
    monkeypatch.setattr(
        plan_module, "get_geocode", lambda *a, **k: ((30.26, -97.74), box)
    )
    built = await build_plan(
        _plans_config([{"keyword": "plumber"}, {"keyword": "hvac", "grid": 2}])
    )
    whole = [page for page in built.pages if page.keyword == "plumber"]
    assert {(page.lat, page.lng) for page in whole} == {(30.26, -97.74)}
    assert whole[0].zoom == 12
    assert built.bounding_box is not None


async def test_a_run_with_no_grid_is_not_trimmed_to_the_city_box(monkeypatch):
    box = [str(v) for v in (30.09, 30.51, -97.93, -97.56)]
    monkeypatch.setattr(
        plan_module, "get_geocode", lambda *a, **k: ((30.26, -97.74), box)
    )
    built = await build_plan(_config())
    assert built.bounding_box is None


async def test_no_geocode_raises(monkeypatch):
    monkeypatch.setattr(plan_module, "get_geocode", lambda *a, **k: None)
    with pytest.raises(SerperGeocodeError):
        await build_plan(_config())


async def test_a_grid_without_a_bounding_box_raises(monkeypatch):
    monkeypatch.setattr(
        plan_module, "get_geocode", lambda *a, **k: ((30.26, -97.74), None)
    )
    with pytest.raises(SerperGeocodeError):
        await build_plan(_config(grid=3))


async def test_a_grid_keyword_without_a_bounding_box_raises_beside_a_whole_city(
    monkeypatch,
):
    monkeypatch.setattr(
        plan_module, "get_geocode", lambda *a, **k: ((30.26, -97.74), None)
    )
    with pytest.raises(SerperGeocodeError):
        await build_plan(
            _plans_config([{"keyword": "plumber"}, {"keyword": "hvac", "grid": 2}])
        )


def test_unknown_fields_are_refused():
    with pytest.raises(ValidationError):
        _config(proxy="http://x")


async def test_a_whole_city_uses_the_zoom_its_own_box_gives(monkeypatch):
    box = [str(v) for v in (30.09, 30.51, -97.93, -97.56)]
    monkeypatch.setattr(
        plan_module, "get_geocode", lambda *a, **k: ((30.26, -97.74), box)
    )
    built = await build_plan(_config())
    # A city box at 1x1 is zoom 12 in build_grid_cells, not a neighbourhood's 15.
    assert built.pages[0].zoom == 12
    assert built.bounding_box is None  # a whole-city run is not trimmed to a box


async def test_a_whole_city_without_a_box_falls_back_to_the_default_zoom(monkeypatch):
    monkeypatch.setattr(
        plan_module, "get_geocode", lambda *a, **k: ((30.26, -97.74), None)
    )
    built = await build_plan(_config())
    assert built.pages[0].zoom == plan_module.WHOLE_CITY_ZOOM
