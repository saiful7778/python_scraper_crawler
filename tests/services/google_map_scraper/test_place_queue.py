import json

import fakeredis
import pytest

from app.services.buffer.client import BufferError
from app.services.google_map_scraper.place_query import PlaceQuery
from app.services.google_map_scraper.place_queue import PlaceJobQueue, run_id


def _query(**overrides) -> PlaceQuery:
    return PlaceQuery.model_validate({"keywords": ["cafe", "bakery"], "city": "Austin", "country": "us", **overrides})


def _job(keyword="cafe") -> dict:
    return {"keyword": keyword, "name": keyword, "settings": {}, "bounding_box": None,
            "cells": None, "next_cell": 0, "rows": [], "kept": [], "finished": 0, "skipped": 0}


@pytest.fixture
def redis():
    return fakeredis.FakeRedis(decode_responses=True)


def test_jobs_come_out_in_fifo_order(redis):
    queue = PlaceJobQueue(redis, _query(), 3600)
    queue.enqueue(_job("cafe"))
    queue.enqueue(_job("bakery"))
    assert queue.peek()["keyword"] == "cafe"


def test_complete_moves_the_head_job_to_done(redis):
    queue = PlaceJobQueue(redis, _query(), 3600)
    queue.enqueue(_job("cafe"))
    queue.enqueue(_job("bakery"))
    queue.complete({"kept": [], "finished": 1, "skipped": 0, "blocked": False})
    assert queue.peek()["keyword"] == "bakery"
    assert queue.done() == [{"kept": [], "finished": 1, "skipped": 0, "blocked": False}]


def test_save_replaces_the_head_job_progress(redis):
    queue = PlaceJobQueue(redis, _query(), 3600)
    queue.enqueue(_job("cafe"))
    job = queue.peek()
    job["next_cell"] = 3
    queue.save(job)
    assert queue.peek()["next_cell"] == 3


def test_clear_removes_everything_and_exists_reports_it(redis):
    queue = PlaceJobQueue(redis, _query(), 3600)
    assert not queue.exists()
    queue.enqueue(_job())
    assert queue.exists()
    queue.clear()
    assert not queue.exists()
    assert queue.peek() is None


def test_same_query_same_run_different_grid_different_run():
    assert run_id(_query()) == run_id(_query())
    assert run_id(_query()) != run_id(_query(grid=2))


def test_redis_error_is_a_buffer_error(redis, monkeypatch):
    from redis.exceptions import ConnectionError as RedisConnectionError

    queue = PlaceJobQueue(redis, _query(), 3600)

    def boom(*args, **kwargs):
        raise RedisConnectionError("down")

    monkeypatch.setattr(redis, "lindex", boom)
    with pytest.raises(BufferError):
        queue.peek()


def test_enqueue_redis_error_is_a_buffer_error(redis, monkeypatch):
    from redis.exceptions import ConnectionError as RedisConnectionError

    queue = PlaceJobQueue(redis, _query(), 3600)

    def boom(*args, **kwargs):
        raise RedisConnectionError("down")

    monkeypatch.setattr(redis, "pipeline", boom)
    with pytest.raises(BufferError):
        queue.enqueue(_job())


def test_keys_use_the_place_queue_prefix(redis):
    queue = PlaceJobQueue(redis, _query(), 3600)
    queue.enqueue(_job())
    run = run_id(_query())
    assert redis.exists(f"place_queue:{run}:jobs") == 1


def test_enqueue_save_and_complete_keep_the_keys_on_a_ttl(redis):
    queue = PlaceJobQueue(redis, _query(), 3600)
    run = run_id(_query())
    jobs_key = f"place_queue:{run}:jobs"
    done_key = f"place_queue:{run}:done"

    queue.enqueue(_job("cafe"))
    assert redis.ttl(jobs_key) > 0

    job = queue.peek()
    job["next_cell"] = 1
    queue.save(job)
    assert redis.ttl(jobs_key) > 0
    if redis.exists(done_key):
        assert redis.ttl(done_key) > 0

    queue.complete({"kept": [], "finished": 1, "skipped": 0, "blocked": False})
    queue.enqueue(_job("bakery"))
    assert redis.ttl(jobs_key) > 0
    assert redis.ttl(done_key) > 0


def test_save_leaves_the_second_job_untouched(redis):
    queue = PlaceJobQueue(redis, _query(), 3600)
    queue.enqueue(_job("cafe"))
    queue.enqueue(_job("bakery"))
    job = queue.peek()
    job["next_cell"] = 5
    queue.save(job)
    run = run_id(_query())
    second = redis.lindex(f"place_queue:{run}:jobs", 1)
    assert json.loads(second) == _job("bakery")


def test_save_refreshes_the_ttl_of_the_done_list(redis):
    queue = PlaceJobQueue(redis, _query(), 3600)
    done_key = f"place_queue:{run_id(_query())}:done"
    queue.enqueue(_job("cafe"))
    queue.enqueue(_job("bakery"))
    queue.complete({"kept": [], "finished": 1, "skipped": 0, "blocked": False})
    redis.expire(done_key, 5)
    queue.save(queue.peek())
    assert redis.ttl(done_key) > 5
