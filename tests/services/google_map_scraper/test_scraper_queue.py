import itertools

import fakeredis
import pytest

from app.services.buffer.client import BufferError
from app.services.google_map_scraper import scraper as scraper_module
from app.services.google_map_scraper.place_query import PlaceQuery
from app.services.google_map_scraper.place_queue import PlaceTileQueue
from app.services.google_map_scraper.scrape.scrape_status import ScrapeStatus
from app.services.google_map_scraper.scraper import (
    PlaceScraper,
    UnsavedLeadsError,
)
from tests.services.google_map_scraper.tile_fakes import (
    Reply,
    TileEnv,
    blocked,
    done,
    lost_chrome,
    proxy_failure,
    row,
)

PROXIES = ("http://a:1", "http://b:2")


def _query(**overrides) -> PlaceQuery:
    data = {"keywords": ["cafe"], "city": "Austin", "country": "us", **overrides}
    return PlaceQuery.model_validate(data)


def _geocode(place, with_bbox=False, country=None):
    if with_bbox:
        return ((1.0, 2.0), ["0", "1", "0", "1"])
    return (1.0, 2.0)


def _tile(tile_id, keyword, **overrides) -> dict:
    return {
        "ID": tile_id,
        "keyword": keyword,
        "name": keyword,
        "settings": {"keywords": [keyword], "proxies": []},
        "bounding_box": None,
        "cell": None,
        "attempts": 0,
        "scrape_id": None,
        **overrides,
    }


@pytest.fixture
def redis():
    return fakeredis.FakeRedis(decode_responses=True)


@pytest.fixture
def arrange(monkeypatch):
    def arrange(behave, carried=()):
        env = TileEnv(behave, carried)
        monkeypatch.setattr(scraper_module, "ScrapeSession", env.factory())
        monkeypatch.setattr(scraper_module, "get_geocode", _geocode)
        return env

    return arrange


def test_a_rate_limited_tile_is_requeued_and_runs_on_a_healthy_slot(arrange, redis):
    env = arrange(lambda kw, slot: blocked("a") if slot == 0 else done("b"))
    result = PlaceScraper(redis).scrape(_query(keywords=["cafes"], proxies=PROXIES))
    assert env.slots_used_for("cafes") == [0, 1]
    assert result.finished == 1 and result.blocked is False


def test_a_tile_never_runs_on_another_slots_proxy(arrange, redis):
    env = arrange(lambda kw, slot: proxy_failure() if slot == 0 else done(kw))
    PlaceScraper(redis).scrape(_query(keywords=["cafes", "bakeries"], proxies=PROXIES))
    assert env.submitted
    for scrape_id, slot in env.submitted:
        assert tuple(env.settings_of(scrape_id)["proxies"]) == (PROXIES[slot],)


def test_a_proxy_failure_stops_only_its_own_slot(arrange, redis):
    env = arrange(lambda kw, slot: proxy_failure() if slot == 0 else done("b"))
    result = PlaceScraper(redis).scrape(_query(keywords=["cafes"], proxies=PROXIES))
    assert env.slots_used_for("cafes") == [0, 1]
    assert result.finished == 1 and result.blocked is False


def test_the_other_slots_keep_running_after_one_slot_is_rate_limited(arrange, redis):
    arrange(lambda kw, slot: blocked("a") if slot == 0 else done(kw))
    result = PlaceScraper(redis).scrape(
        _query(keywords=["cafes", "bakeries", "teas"], proxies=PROXIES)
    )
    assert result.finished == 3 and result.blocked is False


def test_tile_is_skipped_after_one_attempt_per_proxy(arrange, redis):
    arrange(lambda kw, slot: blocked("a"))
    result = PlaceScraper(redis).scrape(_query(keywords=["cafes"], proxies=PROXIES))
    assert result.skipped >= 1
    assert result.blocked is True
    assert not redis.keys("place_queue:*")


def test_interrupt_finishes_the_tiles_in_flight_and_keeps_the_salvage(arrange, redis):
    env = arrange(lambda kw, slot: done(f"{kw}-1", f"{kw}-2"))
    env.interrupt_after = 1
    scraper = PlaceScraper(redis)
    with pytest.raises(KeyboardInterrupt):
        scraper.scrape(_query(keywords=["cafes", "bakeries"], proxies=PROXIES))
    assert len(scraper.salvage_rows) == 4
    assert redis.keys("place_queue:*")


def test_resume_submits_in_flight_scrape_ids_and_does_not_rerun_done_tiles(
    arrange, redis
):
    query = _query(keywords=["cafes", "bakeries"], proxies=PROXIES)
    queue = PlaceTileQueue(redis, query, 3600)
    queue.push(_tile("tile-0", "done-kw"))
    queue.take()
    queue.finish(
        "tile-0",
        {
            "kept": [row("x")],
            "finished": 1,
            "skipped": 0,
            "blocked": False,
            "rows": [row("x")],
        },
    )
    queue.push(_tile("tile-a", "cafes"))
    queue.push(_tile("tile-b", "bakeries"))
    queue.take()
    queue.mark_scrape("tile-a", "scrape-a")
    env = arrange(lambda kw, slot: done(kw), carried=["scrape-a"])
    env.scrapes["scrape-a"] = {
        "name": "cafes",
        "settings": {"keywords": ["cafes"], "proxies": [PROXIES[0]]},
    }

    result = PlaceScraper(redis).scrape(query)

    assert env.resubmitted == ["scrape-a"]
    assert env.slots_used_for("cafes") == [0]
    assert [kw for kw, _ in env.searched] == ["cafes", "bakeries"]
    assert "x" in [r["title"] for r in result.rows]


def test_an_in_flight_tile_without_a_scrape_runs_again(arrange, redis):
    query = _query(keywords=["cafes"])
    queue = PlaceTileQueue(redis, query, 3600)
    queue.push(_tile("tile-a", "cafes"))
    queue.take()
    env = arrange(lambda kw, slot: done(kw))
    result = PlaceScraper(redis).scrape(query)
    assert [kw for kw, _ in env.searched] == ["cafes"]
    assert result.finished == 1


def test_a_carried_scrape_that_matches_no_tile_is_finished_and_its_rows_discarded(
    arrange, redis
):
    env = arrange(
        lambda kw, slot: done("leak") if kw == "old" else done("fresh"),
        carried=["scrape-x"],
    )
    env.scrapes["scrape-x"] = {
        "name": "old",
        "settings": {"keywords": ["old"], "proxies": []},
    }
    result = PlaceScraper(redis).scrape(_query(keywords=["cafes"]))
    assert "scrape-x" in env.finished
    assert [r["title"] for r in result.rows] == ["fresh"]


def test_rows_of_tiles_done_before_a_resume_dedupe_against_new_tiles(arrange, redis):
    query = _query(keywords=["cafes", "bakeries"])
    queue = PlaceTileQueue(redis, query, 3600)
    queue.push(_tile("tile-0", "cafes"))
    queue.push(_tile("tile-1", "bakeries"))
    queue.take()
    queue.finish(
        "tile-0",
        {
            "kept": [row("dup")],
            "finished": 1,
            "skipped": 0,
            "blocked": False,
            "rows": [row("dup")],
        },
    )
    arrange(lambda kw, slot: done("dup"))
    result = PlaceScraper(redis).scrape(query)
    assert [r["title"] for r in result.rows] == ["dup"]


def test_each_keyword_is_its_own_tile_and_rows_come_out_in_keyword_order(
    arrange, redis
):
    env = arrange(lambda kw, slot: done(kw))
    result = PlaceScraper(redis).scrape(_query(keywords=["cafes", "bakeries"]))
    assert [kw for kw, _ in env.searched] == ["cafes", "bakeries"]
    assert [r["title"] for r in result.rows] == ["cafes", "bakeries"]
    assert not redis.keys("place_queue:*")
    assert env.stopped == 1


def test_a_lead_found_by_one_tile_is_not_kept_again_by_the_next(arrange, redis):
    arrange(lambda kw, slot: done("same"))
    result = PlaceScraper(redis).scrape(_query(keywords=["cafes", "bakeries"]))
    assert [r["title"] for r in result.rows] == ["same"]


def test_grid_runs_one_tile_per_cell_for_each_keyword(arrange, redis):
    env = arrange(lambda kw, slot: done())
    PlaceScraper(redis).scrape(_query(keywords=["cafes", "bakeries"], grid=2))
    keywords = [kw for kw, _ in env.searched]
    assert keywords.count("cafes") == 4
    assert keywords.count("bakeries") == 4
    assert keywords[:4] == ["cafes"] * 4


def test_limit_applies_to_each_tile_not_the_whole_grid(arrange, redis):
    counter = itertools.count()

    def behave(kw, slot):
        tile = next(counter)
        return done(f"t{tile}-a", f"t{tile}-b")

    env = arrange(behave)
    result = PlaceScraper(redis).scrape(_query(grid=2, limit=1, keywords=["cafes"]))
    assert len(env.searched) == 4
    assert len(result.rows) == 4


def test_deep_scan_splits_a_saturated_tile_once(arrange, redis):
    counter = itertools.count()

    def behave(kw, slot):
        return Reply(
            rows=[row(f"r{next(counter)}") for _ in range(scraper_module.SATURATED)]
        )

    env = arrange(behave)
    PlaceScraper(redis).scrape(_query(grid=2, deep_scan=True, keywords=["cafes"]))
    assert (
        len(env.searched) == 4 + 4 * 4
    )  # four cells, each split once into four sub-tiles


def test_a_failed_tile_keeps_its_rows(arrange, redis):
    arrange(
        lambda kw, slot: Reply(
            status=ScrapeStatus.FAILED, note="boom", rows=[row("failed-tile")]
        )
    )
    result = PlaceScraper(redis).scrape(_query(grid=2, keywords=["cafes"]))
    assert "failed-tile" in [r["title"] for r in result.rows]
    assert result.skipped == 4


def test_a_blocked_tile_keeps_its_rows_when_it_is_skipped(arrange, redis):
    arrange(lambda kw, slot: blocked("blocked-tile"))
    scraper = PlaceScraper(redis)
    result = scraper.scrape(_query(grid=2, keywords=["cafes"]))
    assert result.blocked
    assert "blocked-tile" in [r["title"] for r in result.rows]
    assert "blocked-tile" in [r["title"] for r in scraper.salvage_rows]


def test_a_lost_chrome_connection_stops_the_run_and_keeps_the_queue(arrange, redis):
    env = arrange(lambda kw, slot: lost_chrome())
    result = PlaceScraper(redis).scrape(_query(keywords=["cafes", "bakeries"]))
    assert [kw for kw, _ in env.searched] == ["cafes"]
    assert result.skipped == 1
    assert redis.keys("place_queue:*")


def test_a_rate_limit_ends_the_run_and_clears_the_queue(arrange, redis):
    env = arrange(lambda kw, slot: blocked("a"))
    result = PlaceScraper(redis).scrape(_query(keywords=["cafes", "bakeries"]))
    assert result.blocked
    assert [kw for kw, _ in env.searched] == ["cafes"]
    assert not redis.keys("place_queue:*")


def test_a_deleted_tile_is_skipped_and_the_next_one_runs(arrange, redis):
    arrange(lambda kw, slot: Reply(deleted=True) if kw == "cafes" else done(kw))
    result = PlaceScraper(redis).scrape(_query(keywords=["cafes", "bakeries"]))
    assert result.skipped == 1
    assert [r["title"] for r in result.rows] == ["bakeries"]


def test_unsaved_leads_raise_and_keep_the_queue(arrange, redis):
    arrange(lambda kw, slot: Reply(unsaved=True, rows=[row("a")]))
    with pytest.raises(UnsavedLeadsError):
        PlaceScraper(redis).scrape(_query(keywords=["cafes", "bakeries"]))
    assert redis.keys("place_queue:*")


def test_a_crash_in_a_cell_resumes_at_that_cell(arrange, redis):
    calls = []

    def behave(kw, slot):
        calls.append(kw)
        if len(calls) == 2:
            raise BufferError("redis down")
        return done()

    env = arrange(behave)
    with pytest.raises(BufferError):
        PlaceScraper(redis).scrape(_query(grid=2, keywords=["cafes"]))

    env.carried = ["scrape-2"]
    env.searched.clear()
    env.behave = lambda kw, slot: done()
    PlaceScraper(redis).scrape(_query(grid=2, keywords=["cafes"]))
    assert env.resubmitted == ["scrape-2"]
    assert len(env.searched) == 3  # cell 2 runs again, then cells 3 and 4


def test_a_crash_keeps_the_queue_for_the_next_run(arrange, redis):
    def behave(kw, slot):
        raise BufferError("redis down")

    arrange(behave)
    with pytest.raises(BufferError):
        PlaceScraper(redis).scrape(_query(keywords=["cafes"]))
    assert redis.keys("place_queue:*")


def test_a_geocoding_failure_leaves_no_queue(monkeypatch, redis):
    monkeypatch.setattr(scraper_module, "get_geocode", lambda *a, **k: None)
    result = PlaceScraper(redis).scrape(_query(keywords=["cafes"]))
    assert result.rows == []
    assert not redis.keys("place_queue:*")


def test_a_failed_enqueue_leaves_no_queue(arrange, monkeypatch, redis):
    arrange(lambda kw, slot: done())
    real_push = PlaceTileQueue.push
    count = []

    def push_then_fail(self, tile):
        if count:
            raise BufferError("redis down")
        count.append(True)
        real_push(self, tile)

    monkeypatch.setattr(PlaceTileQueue, "push", push_then_fail)
    with pytest.raises(BufferError):
        PlaceScraper(redis).scrape(_query(keywords=["cafes", "bakeries"]))
    assert not redis.keys("place_queue:*")


def test_a_failed_clear_after_a_failed_enqueue_keeps_the_original_error(
    arrange, monkeypatch, redis
):
    arrange(lambda kw, slot: done())

    def push_fails(self, tile):
        raise BufferError("enqueue down")

    def clear_fails(self):
        raise BufferError("clear down")

    monkeypatch.setattr(PlaceTileQueue, "push", push_fails)
    monkeypatch.setattr(PlaceTileQueue, "clear", clear_fails)
    with pytest.raises(BufferError, match="enqueue down"):
        PlaceScraper(redis).scrape(_query(keywords=["cafes"]))


def test_ctrl_c_lets_the_one_tile_in_flight_finish_and_takes_no_more(arrange, redis):
    scraper = PlaceScraper(redis)
    env = arrange(lambda kw, slot: done(kw))
    env.interrupt_after = 0
    with pytest.raises(KeyboardInterrupt):
        scraper.scrape(_query(grid=2, keywords=["cafes"]))
    assert len(env.searched) == 1
    assert len(scraper.salvage_rows) == 1
    assert (
        len(PlaceTileQueue(redis, _query(grid=2, keywords=["cafes"]), 3600).pending())
        == 3
    )


def test_a_tile_deleted_before_its_worker_claims_it_is_skipped_and_the_run_ends(
    arrange, redis
):
    env = arrange(lambda kw, slot: done(kw))
    env.deleted.add(
        "scrape-1"
    )  # the first tile's scrape is deleted before any worker runs it
    result = PlaceScraper(redis).scrape(_query(keywords=["cafes", "bakeries"]))
    assert result.skipped == 1
    assert [r["title"] for r in result.rows] == ["bakeries"]
    assert not redis.keys("place_queue:*")
