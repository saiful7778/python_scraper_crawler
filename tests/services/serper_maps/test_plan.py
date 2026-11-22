import pytest
from pydantic import ValidationError

from app.services.serper.maps import serper_map_plan as plan_module
from app.services.serper.maps.serper_map_config import SerperMapConfig
from app.services.serper.maps.serper_map_errors import SerperGeocodeError
from app.services.serper.maps.serper_map_plan import build_plan


def _config(**overrides):
    values = {"keywords": ("plumbers",), "city": "Austin", "state": "TX", "country": "US"}
    values.update(overrides)
    return SerperMapConfig(**values)


def test_defaults_are_one_page_one_tile_and_five_parallel():
    config = _config()
    assert (config.depth, config.grid, config.parallel) == (1, 1, 5)
    assert config.place == "Austin, TX, US"
    assert config.name == "plumbers @ Austin, TX, US"


def test_country_is_lowercased_and_depth_and_grid_are_bounded():
    assert _config(country="US").country == "us"
    with pytest.raises(ValidationError):
        _config(depth=0)
    with pytest.raises(ValidationError):
        _config(grid=11)
    with pytest.raises(ValidationError):
        _config(parallel=21)


def test_the_config_key_ignores_parallel_but_not_the_search():
    base = _config(depth=2, grid=3)
    assert base.config_key == _config(depth=2, grid=3, parallel=9).config_key
    assert base.config_key != _config(depth=2, grid=4).config_key
    assert base.config_key != _config(has_phone=True, depth=2, grid=3).config_key


def test_the_place_leaves_out_a_missing_state():
    assert _config(state=None).place == "Austin, US"


async def test_a_whole_city_is_one_tile_with_every_page(monkeypatch):
    monkeypatch.setattr(plan_module, "get_geocode", lambda *a, **k: ((30.26, -97.74), None))
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
    assert built.tile_count == 9
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


async def test_no_geocode_raises(monkeypatch):
    monkeypatch.setattr(plan_module, "get_geocode", lambda *a, **k: None)
    with pytest.raises(SerperGeocodeError):
        await build_plan(_config())


async def test_a_grid_without_a_bounding_box_raises(monkeypatch):
    monkeypatch.setattr(plan_module, "get_geocode", lambda *a, **k: ((30.26, -97.74), None))
    with pytest.raises(SerperGeocodeError):
        await build_plan(_config(grid=3))


def test_unknown_fields_are_refused():
    with pytest.raises(ValidationError):
        _config(proxy="http://x")
