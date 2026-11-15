import fakeredis
import pytest

from app.services.buffer.client import BufferError
from app.services.google_map_scraper.place_query import PlaceQuery
from app.services.google_map_scraper.place_queue import PlaceTileQueue, run_id


def _query(**overrides) -> PlaceQuery:
    return PlaceQuery.model_validate(
        {"keywords": ["cafe", "bakery"], "city": "Austin", "country": "us", **overrides}
    )


def _tile(tile_id="tile-1", keyword="cafe", **overrides) -> dict:
    return {
        "ID": tile_id,
        "keyword": keyword,
        "keyword_index": 1,
        "cell_index": None,
        "name": keyword,
        "settings": {"keywords": [keyword], "proxies": []},
        "bounding_box": None,
        "cell": None,
        "attempts": 0,
        "scrape_id": None,
        **overrides,
    }


def _done(**overrides) -> dict:
    return {
        "keyword_index": 1,
        "kept": [],
        "finished": 1,
        "skipped": 0,
        "blocked": False,
        "rows": [],
        **overrides,
    }


@pytest.fixture
def redis():
    return fakeredis.FakeRedis(decode_responses=True)


def test_take_returns_tiles_in_fifo_order_and_marks_them_in_flight(redis):
    queue = PlaceTileQueue(redis, _query(), 3600)
    queue.push(_tile("tile-1"))
    queue.push(_tile("tile-2"))
    assert queue.take()["ID"] == "tile-1"
    assert [tile["ID"] for tile in queue.in_flight()] == ["tile-1"]
    assert [tile["ID"] for tile in queue.pending()] == ["tile-2"]


def test_take_on_an_empty_queue_is_none(redis):
    queue = PlaceTileQueue(redis, _query(), 3600)
    assert queue.take() is None


def test_a_tile_is_never_taken_twice(redis):
    queue = PlaceTileQueue(redis, _query(), 3600)
    queue.push(_tile("tile-1"))
    assert queue.take() is not None
    assert queue.take() is None


def test_push_front_puts_a_requeued_tile_first(redis):
    queue = PlaceTileQueue(redis, _query(), 3600)
    queue.push(_tile("tile-1"))
    queue.push(_tile("tile-2"))
    queue.push_front(_tile("tile-0"))
    assert queue.take()["ID"] == "tile-0"


def test_mark_scrape_stores_the_scrape_id_on_the_in_flight_tile(redis):
    queue = PlaceTileQueue(redis, _query(), 3600)
    queue.push(_tile("tile-1"))
    queue.take()
    queue.mark_scrape("tile-1", "scrape-a")
    assert queue.in_flight()[0]["scrape_id"] == "scrape-a"


def test_mark_scrape_on_a_tile_not_in_flight_is_a_buffer_error(redis):
    queue = PlaceTileQueue(redis, _query(), 3600)
    with pytest.raises(BufferError):
        queue.mark_scrape("missing", "scrape-a")


def test_finish_removes_the_tile_from_in_flight_and_appends_it_to_done(redis):
    queue = PlaceTileQueue(redis, _query(), 3600)
    queue.push(_tile("tile-1"))
    queue.take()
    done = _done(kept=[{"title": "a"}], rows=[{"title": "a"}])
    queue.finish("tile-1", done)
    assert queue.in_flight() == []
    assert queue.done() == [done]


def test_requeue_moves_an_in_flight_tile_back_to_the_front_with_its_attempts(redis):
    queue = PlaceTileQueue(redis, _query(), 3600)
    queue.push(_tile("tile-1"))
    queue.push(_tile("tile-2"))
    queue.take()
    queue.mark_scrape("tile-1", "scrape-a")
    queue.requeue("tile-1", _tile("tile-1", attempts=1, scrape_id=None))
    assert queue.in_flight() == []
    assert [tile["ID"] for tile in queue.pending()] == ["tile-1", "tile-2"]
    assert queue.pending()[0]["attempts"] == 1


def test_exists_reports_a_run_with_pending_or_done_tiles_and_clear_removes_them(redis):
    queue = PlaceTileQueue(redis, _query(), 3600)
    assert not queue.exists()
    queue.push(_tile())
    assert queue.exists()
    queue.clear()
    assert not queue.exists()
    assert queue.take() is None


def test_done_tiles_survive_a_new_queue_object(redis):
    first = PlaceTileQueue(redis, _query(), 3600)
    first.push(_tile("tile-1"))
    first.take()
    first.finish("tile-1", _done(rows=[{"title": "a"}]))
    second = PlaceTileQueue(redis, _query(), 3600)
    assert second.exists()
    assert second.done() == [_done(rows=[{"title": "a"}])]


def test_same_query_same_run_different_grid_different_run():
    assert run_id(_query()) == run_id(_query())
    assert run_id(_query()) != run_id(_query(grid=2))


def test_a_different_proxy_list_is_a_different_run():
    assert run_id(_query()) != run_id(_query(proxies=("http://a:1",)))


def test_keys_use_the_place_queue_prefix(redis):
    queue = PlaceTileQueue(redis, _query(), 3600)
    queue.push(_tile())
    assert redis.exists(f"place_queue:{run_id(_query())}:pending") == 1


def test_push_and_take_keep_the_keys_on_a_ttl(redis):
    queue = PlaceTileQueue(redis, _query(), 3600)
    pending_key = f"place_queue:{run_id(_query())}:pending"
    in_flight_key = f"place_queue:{run_id(_query())}:in_flight"
    queue.push(_tile("tile-1"))
    queue.push(_tile("tile-2"))
    queue.take()
    assert redis.ttl(pending_key) > 0
    assert redis.ttl(in_flight_key) > 0


def test_redis_error_is_a_buffer_error(redis, monkeypatch):
    from redis.exceptions import ConnectionError as RedisConnectionError

    queue = PlaceTileQueue(redis, _query(), 3600)
    queue.push(_tile())

    def boom(*args, **kwargs):
        raise RedisConnectionError("down")

    monkeypatch.setattr(redis, "lmove", boom)
    with pytest.raises(BufferError):
        queue.take()


def test_enqueue_redis_error_is_a_buffer_error(redis, monkeypatch):
    from redis.exceptions import ConnectionError as RedisConnectionError

    queue = PlaceTileQueue(redis, _query(), 3600)

    def boom(*args, **kwargs):
        raise RedisConnectionError("down")

    monkeypatch.setattr(redis, "pipeline", boom)
    with pytest.raises(BufferError):
        queue.push(_tile())
