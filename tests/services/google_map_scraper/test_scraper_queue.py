import itertools
import threading
import time
from typing import cast

import fakeredis
import pytest

from app.services.buffer.client import BufferError
from app.services.google_map_scraper import scraper as scraper_module
from app.services.google_map_scraper.place_query import PlaceQuery
from app.services.google_map_scraper.place_queue import PlaceTile, PlaceTileQueue
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
    failed,
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


def _cell() -> dict:
    return {
        "lat": 0.25,
        "lon": 0.25,
        "radius": 100,
        "zoom": 15,
        "_bbox": [0.0, 0.5, 0.0, 0.5],
    }


def _tile(tile_id, keyword, cell_index=None, **overrides) -> PlaceTile:
    return cast(
        PlaceTile,
        {
            "ID": tile_id,
            "keyword": keyword,
            "keyword_index": 1,
            "cell_index": cell_index,
            "name": keyword,
            "settings": {"keywords": [keyword], "proxies": []},
            "bounding_box": None,
            "cell": None if cell_index is None else _cell(),
            "attempts": 0,
            "scrape_id": None,
            **overrides,
        },
    )


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


def test_a_requeued_tile_keeps_only_the_limit_across_its_attempts(arrange, redis):
    env = arrange(
        lambda kw, slot: blocked("a", "b") if slot == 0 else done("c", "d", "e")
    )
    result = PlaceScraper(redis).scrape(
        _query(keywords=["cafes"], proxies=PROXIES, limit=2)
    )
    assert env.slots_used_for("cafes") == [0, 1]
    assert [r["title"] for r in result.rows] == ["a", "b"]


def test_a_requeued_tile_keeps_its_rows_and_its_limit_across_a_restart(arrange, redis):
    env = arrange(lambda kw, slot: done("c", "d"))
    query = _query(keywords=["cafes"], proxies=PROXIES, limit=2)
    queue = PlaceTileQueue(redis, query, 60)
    tile = _tile("tile-1", "cafes")
    queue.push(tile)
    queue.take()
    queue.requeue(
        "tile-1",
        cast(
            PlaceTile,
            {**tile, "attempts": 1, "kept": [row("a")], "rows": [row("a")]},
        ),
    )
    result = PlaceScraper(redis).scrape(query)
    assert env.slots_used_for("cafes") == [0]
    assert [r["title"] for r in result.rows] == ["a", "c"]


def test_rows_found_before_a_block_survive_when_no_slot_can_retry(arrange, redis):
    env = arrange(lambda kw, slot: blocked(kw))
    result = PlaceScraper(redis).scrape(
        _query(keywords=["cafes", "bakeries"], proxies=PROXIES)
    )
    assert sorted(r["title"] for r in result.rows) == ["bakeries", "cafes"]
    assert env.slots_used_for("cafes") == [0]
    assert env.slots_used_for("bakeries") == [1]


def test_a_tile_never_runs_on_another_slots_proxy(arrange, redis):
    env = arrange(lambda kw, slot: proxy_failure() if slot == 0 else done(kw))
    PlaceScraper(redis).scrape(_query(keywords=["cafes", "bakeries"], proxies=PROXIES))
    assert env.submitted
    for scrape_id, slot in env.submitted:
        assert tuple(env.settings_of(scrape_id)["proxies"]) == (PROXIES[slot],)


def test_no_proxies_means_one_slot_without_proxy(arrange, redis):
    env = arrange(lambda kw, slot: done(kw))
    PlaceScraper(redis).scrape(_query(keywords=["cafes", "bakeries"]))
    assert env.slot_counts == [1]
    assert env.submitted
    assert all(tuple(env.settings_of(sid)["proxies"]) == () for sid, _ in env.submitted)


def test_a_has_website_run_checks_crawlability_in_the_queue(arrange, redis):
    env = arrange(lambda kw, slot: done(kw))
    PlaceScraper(redis).scrape(_query(has_website=True))
    assert env.check_crawlable == [True]


def test_a_proxy_failure_stops_only_its_own_slot(arrange, redis):
    env = arrange(lambda kw, slot: proxy_failure() if slot == 0 else done("b"))
    result = PlaceScraper(redis).scrape(_query(keywords=["cafes"], proxies=PROXIES))
    assert env.slots_used_for("cafes") == [0, 1]
    assert result.finished == 1 and result.blocked is False


def test_a_navigation_timeout_requeues_the_tile_on_a_healthy_slot(arrange, redis):
    env = arrange(
        lambda kw, slot: (
            failed("CDPError: no Page.loadEventFired event within 45.0 s")
            if slot == 0
            else done("b")
        )
    )
    result = PlaceScraper(redis).scrape(_query(keywords=["cafes"], proxies=PROXIES))
    assert env.slots_used_for("cafes") == [0, 1]
    assert result.finished == 1 and result.blocked is False


def test_navigation_timeouts_do_not_retire_every_slot_and_stop_the_run(arrange, redis):
    attempts = itertools.count()

    def behave(kw, slot):
        if next(attempts) < 2:
            return failed("CDPError: no Page.loadEventFired event within 45.0 s")
        return done(kw)

    arrange(behave)
    result = PlaceScraper(redis).scrape(
        _query(keywords=["cafes", "bakeries", "teas"], proxies=PROXIES)
    )
    assert result.finished == 3 and result.skipped == 0


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
            "keyword_index": 1,
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
            "keyword_index": 1,
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


def test_a_slot_takes_another_tile_while_the_finished_one_is_still_saving(
    arrange, redis
):
    env = arrange(lambda kw, slot: done(kw))
    env.finish_gate = threading.Event()

    run = threading.Thread(
        target=lambda: PlaceScraper(redis).scrape(
            _query(keywords=["cafes", "bakeries", "pubs"], proxies=PROXIES)
        )
    )
    run.start()
    try:
        assert env.finish_started.wait(5)  # a save is now holding the finish pool
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and len({kw for kw, _ in env.searched}) < 3:
            time.sleep(0.01)
        assert {kw for kw, _ in env.searched} == {"cafes", "bakeries", "pubs"}
    finally:
        env.finish_gate.set()
        run.join(5)
    assert not run.is_alive()


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


def test_a_deleted_flight_cancels_the_worker_that_runs_it(arrange, redis):
    env = arrange(lambda kw, slot: done(kw))
    env.deleted.add("scrape-1")
    PlaceScraper(redis).scrape(_query(keywords=["cafes", "bakeries"]))
    assert env.cancelled == ["scrape-1"]


def test_the_connection_flag_alone_stops_the_run_and_keeps_the_queue(arrange, redis):
    env = arrange(
        lambda kw, slot: Reply(
            status=ScrapeStatus.FAILED, note="boom", connection_lost=True
        )
    )
    result = PlaceScraper(redis).scrape(_query(keywords=["cafes", "bakeries"]))
    assert [kw for kw, _ in env.searched] == ["cafes"]
    assert result.skipped == 1
    assert redis.keys("place_queue:*")


def _lines(capsys) -> list[str]:
    return capsys.readouterr().out.splitlines()


def test_the_no_proxy_single_keyword_run_prints_the_base_lines(arrange, redis, capsys):
    arrange(lambda kw, slot: done("a"))
    query = _query(keywords=["cafes"])
    PlaceScraper(redis).scrape(query)
    lines = _lines(capsys)
    assert "▶ Keyword 1/1: cafes" in lines
    assert f"▶ Creating scrape: 1 keyword @ 1.0,2.0 depth={query.scroll_depth}" in lines


def test_the_no_proxy_grid_run_prints_the_base_lines(arrange, redis, capsys):
    counter = itertools.count()
    arrange(lambda kw, slot: done(f"t{next(counter)}"))
    query = _query(keywords=["cafes"], grid=2)
    PlaceScraper(redis).scrape(query)
    lines = _lines(capsys)
    assert "▶ Keyword 1/1: cafes" in lines
    assert f"▶ Grid 2x2: 4 scrapes, one at a time, depth={query.scroll_depth}" in lines
    assert any(
        line.startswith("▶ Cell 1/4 @ ") and line.endswith("m") for line in lines
    )
    assert "cell 1/4: 1 rows, 1 kept so far" in lines
    assert "cell 4/4: 1 rows, 4 kept so far" in lines
    assert "4 unique rows, 4 after the city box and filters" in lines


def test_the_no_proxy_lost_chrome_and_blocked_cells_print_the_base_lines(
    arrange, redis, capsys
):
    arrange(lambda kw, slot: lost_chrome())
    PlaceScraper(redis).scrape(_query(keywords=["cafes"], grid=2))
    lines = _lines(capsys)
    assert (
        "✗ Stopped at cell 1/4: Chrome connection lost (CDPError: navigate: connection lost: gone)"
        in lines
    )
    assert "0 unique rows, 0 after the city box and filters" in lines


def test_the_no_proxy_blocked_cell_and_failed_cell_print_the_base_lines(
    arrange, redis, capsys
):
    arrange(lambda kw, slot: blocked())
    PlaceScraper(redis).scrape(_query(keywords=["cafes"], grid=2))
    assert (
        "\n".join(_lines(capsys)).count(
            "⚠ Stopped at cell 1/4: Google rate-limited this IP; the cells after it were not searched."
        )
        == 1
    )

    arrange(lambda kw, slot: failed("boom"))
    PlaceScraper(redis).scrape(_query(keywords=["cafes"], grid=2))
    assert "  ⚠ cell 1/4 skipped: boom" in _lines(capsys)


def test_the_no_proxy_interrupt_prints_the_base_message(arrange, redis, capsys):
    env = arrange(lambda kw, slot: done("a"))
    env.interrupt_after = 0
    with pytest.raises(KeyboardInterrupt):
        PlaceScraper(redis).scrape(_query(keywords=["cafes"], grid=2))
    assert "⏸ Finishing the current cell, press Ctrl-C again to quit" in _lines(capsys)


def test_the_done_record_is_written_before_the_scrape_is_saved(
    arrange, redis, monkeypatch
):
    from tests.services.google_map_scraper.tile_fakes import TileSession

    query = _query(keywords=["cafes"])
    seen = []
    real_finish = TileSession.finish

    def finish_checking_queue(self, scrape_id):
        seen.append(
            [
                item["keyword_index"]
                for item in PlaceTileQueue(redis, query, 3600).done()
            ]
        )
        return real_finish(self, scrape_id)

    monkeypatch.setattr(TileSession, "finish", finish_checking_queue)
    arrange(lambda kw, slot: done("a"))
    PlaceScraper(redis).scrape(query)
    assert seen == [[1]]


def test_a_crash_between_the_done_record_and_the_save_does_not_search_the_tile_again(
    arrange, redis, monkeypatch
):
    from tests.services.google_map_scraper.tile_fakes import TileSession

    query = _query(keywords=["cafes", "bakeries"])
    real_finish = TileSession.finish
    crashed = []

    def crash_once(self, scrape_id):
        if not crashed:
            crashed.append(scrape_id)
            raise BufferError("process died before the save")
        return real_finish(self, scrape_id)

    monkeypatch.setattr(TileSession, "finish", crash_once)
    arrange(lambda kw, slot: done(kw))
    with pytest.raises(BufferError):
        PlaceScraper(redis).scrape(query)
    monkeypatch.setattr(TileSession, "finish", real_finish)

    # the next start: the crashed scrape is still interrupted, so the session carries it
    env = arrange(lambda kw, slot: done(kw), carried=crashed)
    env.scrapes[crashed[0]] = {
        "name": "cafes",
        "settings": {"keywords": ["cafes"], "proxies": []},
    }
    result = PlaceScraper(redis).scrape(query)

    assert not [
        sid
        for sid, s in env.scrapes.items()
        if sid != crashed[0] and s["settings"]["keywords"] == ["cafes"]
    ]
    assert "cafes" in [r["title"] for r in result.rows]


def test_a_keyword_dedupes_against_every_row_it_found_not_only_the_kept_ones(
    arrange, redis
):
    replies = [[row("A"), row("B")], [row("B")]]
    arrange(lambda kw, slot: Reply(rows=replies.pop(0) if replies else []))
    result = PlaceScraper(redis).scrape(_query(keywords=["cafes"], grid=2, limit=1))
    assert [r["title"] for r in result.rows] == ["A"]


def test_an_earlier_keyword_blocks_only_the_rows_it_kept(arrange, redis):
    replies = [[row("A"), row("B")], [row("B")]]
    arrange(lambda kw, slot: Reply(rows=replies.pop(0)))
    result = PlaceScraper(redis).scrape(_query(keywords=["cafes", "bakeries"], limit=1))
    assert [r["title"] for r in result.rows] == ["A", "B"]


def test_a_resumed_keyword_dedupes_against_the_rows_its_done_tiles_found(
    arrange, redis
):
    query = _query(keywords=["cafes"], grid=2, limit=1)
    queue = PlaceTileQueue(redis, query, 3600)
    queue.push(_tile("tile-0", "cafes", cell_index=1))
    queue.push(_tile("tile-1", "cafes", cell_index=2))
    queue.take()
    queue.finish(
        "tile-0",
        {
            "keyword_index": 1,
            "kept": [row("A")],
            "finished": 1,
            "skipped": 0,
            "blocked": False,
            "rows": [row("A"), row("B")],
        },
    )
    arrange(lambda kw, slot: Reply(rows=[row("B")]))
    result = PlaceScraper(redis).scrape(query)
    assert [r["title"] for r in result.rows] == ["A"]


def test_a_lost_chrome_on_a_proxy_run_also_stops_the_run_and_keeps_the_queue(
    arrange, redis
):
    env = arrange(lambda kw, slot: lost_chrome())
    result = PlaceScraper(redis).scrape(
        _query(keywords=["cafes", "bakeries", "teas"], proxies=PROXIES)
    )
    # the two tiles already in flight stop with the Chrome loss; the third is never taken
    assert sorted(kw for kw, _ in env.searched) == ["bakeries", "cafes"]
    assert result.skipped == 2
    assert redis.keys("place_queue:*")


def test_a_proxy_run_dedupes_against_every_kept_row_when_a_later_keyword_finishes_first(
    arrange, redis
):
    env = arrange(lambda kw, slot: done("shared"))
    env.deliver_last_first = True
    result = PlaceScraper(redis).scrape(
        _query(keywords=["cafes", "bakeries"], proxies=PROXIES)
    )
    assert [kw for kw, _ in env.searched] == ["bakeries", "cafes"]
    assert [r["title"] for r in result.rows] == ["shared"]


def test_the_done_record_is_written_before_a_blocked_tile_is_saved(
    arrange, redis, monkeypatch
):
    from tests.services.google_map_scraper.tile_fakes import TileSession

    query = _query(keywords=["cafes"])
    seen = []
    real_finish = TileSession.finish

    def finish_checking_queue(self, scrape_id):
        seen.append(len(PlaceTileQueue(redis, query, 3600).done()))
        return real_finish(self, scrape_id)

    monkeypatch.setattr(TileSession, "finish", finish_checking_queue)
    arrange(lambda kw, slot: blocked("a"))
    PlaceScraper(redis).scrape(query)
    assert seen == [1]


def _planned(*plans, **overrides) -> PlaceQuery:
    return PlaceQuery.model_validate(
        {
            "plans": [
                {"keyword": keyword, "depth": depth, "grid": grid}
                for keyword, depth, grid in plans
            ],
            "city": "Austin",
            "country": "us",
            **overrides,
        }
    )


def test_each_keyword_runs_with_its_own_grid(arrange, redis):
    env = arrange(lambda kw, slot: done(kw))
    PlaceScraper(redis).scrape(_planned(("cafes", 5, 1), ("pizza", 5, 2)))
    assert [kw for kw, _ in env.searched] == [
        "cafes",
        "pizza",
        "pizza",
        "pizza",
        "pizza",
    ]


def test_each_tile_carries_its_keywords_depth(arrange, redis):
    env = arrange(lambda kw, slot: done(kw))
    PlaceScraper(redis).scrape(_planned(("cafes", 4, 1), ("pizza", 6, 2)))
    depths = {
        env._keyword(sid): env.settings_of(sid)["depth"] for sid, _ in env.submitted
    }
    assert depths == {"cafes": 4, "pizza": 6}


def test_the_whole_city_keyword_of_a_mixed_run_prints_no_cell_lines(
    arrange, redis, capsys
):
    counter = itertools.count()
    arrange(lambda kw, slot: done(f"t{next(counter)}"))
    PlaceScraper(redis).scrape(_planned(("cafes", 4, 1), ("pizza", 6, 2)))
    lines = _lines(capsys)
    assert "▶ Creating scrape: 1 keyword @ 1.0,2.0 depth=4" in lines
    assert "▶ Grid 2x2: 4 scrapes, one at a time, depth=6" in lines
    # only the tiled keyword prints cells, and all four of them are its own
    cells = [line for line in lines if line.startswith("▶ Cell ")]
    assert len(cells) == 4
    assert all(
        line.startswith("▶ Cell ") and line.split()[2].split("/")[1] == "4"
        for line in cells
    )
    assert [line for line in lines if "unique rows" in line] == [
        "4 unique rows, 4 after the city box and filters"
    ]


def test_a_whole_city_run_does_not_need_a_bounding_box(arrange, redis, monkeypatch):
    arrange(lambda kw, slot: done(kw))
    asked = []

    def geocode(place, with_bbox=False, country=None):
        asked.append(with_bbox)
        return _geocode(place, with_bbox=with_bbox, country=country)

    monkeypatch.setattr(scraper_module, "get_geocode", geocode)
    PlaceScraper(redis).scrape(_planned(("cafes", 5, 1)))
    PlaceScraper(redis).scrape(_planned(("teas", 5, 2)))
    assert asked == [False, True]


def test_a_whole_city_keyword_of_a_mixed_run_is_not_trimmed_to_the_city_box(
    arrange, redis
):
    def behave(kw, slot):
        if kw == "cafes":
            return Reply(rows=[row("far-cafes", latitude=50.0, longitude=50.0)])
        return done(f"{kw}-{slot}")

    arrange(behave)
    result = PlaceScraper(redis).scrape(_planned(("cafes", 5, 1), ("pizza", 5, 2)))
    assert "far-cafes" in [r["title"] for r in result.rows]
