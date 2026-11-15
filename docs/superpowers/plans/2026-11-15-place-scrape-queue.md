# Place scrape queue Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Run each city's keyword scrapes through a Redis-backed FIFO queue, so an interrupted run resumes at the grid cell it stopped at.

**Architecture:** A new `PlaceJobQueue` class stores one JSON job per keyword in a Redis list, plus a list of finished keywords. `PlaceScraper` enqueues jobs in `_scrape_keyword`, and a listener in the calling thread runs the head job on its own thread, saving progress before each grid cell.

**Tech Stack:** Python 3.12, redis-py, fakeredis (tests), pytest.

**Spec:** `docs/superpowers/specs/2026-11-15-place-scrape-queue-design.md`

## Global Constraints

- Queue keys are `place_queue:<run id>:jobs` and `place_queue:<run id>:done`, with TTL `BUFFER_TTL_SECONDS`.
- Redis errors surface as `BufferError`.
- One job runs at a time; each job runs on a daemon thread.
- `scrape()` keeps its signature and returns the same `ScrapeResult`.

## Review Focus

- Re-running after a crash mid-cell re-runs that cell only; finished keywords are not searched again. (Task 2)
- A non-grid keyword that crashes is re-run whole on resume. (Task 2)
- A run that ends with a rate limit clears its queue. (Task 2)
- A crash leaves the queue in place, so the next run resumes it. (Task 2)
- A different query (different grid) does not resume the old queue. (Task 1)

---

### Task 1: PlaceJobQueue

**Files:**
- Create: `app/services/google_map_scraper/place_queue.py`
- Test: `tests/services/google_map_scraper/test_place_queue.py`

**Interfaces:**
- Produces:
  - `class PlaceJob(TypedDict)`: `keyword: str`, `name: str`, `settings: dict`, `bounding_box: list[float] | None`, `cells: list[dict] | None`, `next_cell: int`, `rows: list[dict]`, `kept: list[dict]`, `finished: int`, `skipped: int`
  - `class DoneKeyword(TypedDict)`: `kept: list[dict]`, `finished: int`, `skipped: int`, `blocked: bool`
  - `run_id(query: PlaceQuery) -> str`: sha256 hex of `query.model_dump(mode="json")` with sorted keys
  - `class PlaceJobQueue(redis: Redis, query: PlaceQuery, ttl_seconds: int)` with:
    - `exists() -> bool`: either key present
    - `enqueue(job: PlaceJob) -> None`: RPUSH to jobs
    - `peek() -> PlaceJob | None`: LINDEX 0
    - `save(job: PlaceJob) -> None`: LSET 0 (only called while the job is at the head)
    - `complete(done: DoneKeyword) -> None`: one transaction: RPUSH done, LPOP jobs, EXPIRE both
    - `done() -> list[DoneKeyword]`: all done records in order
    - `clear() -> None`: DELETE both keys
  - Every method wraps `redis.exceptions.RedisError` in `BufferError`.

- [ ] **Step 1: Write the failing tests**

```python
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
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/services/google_map_scraper/test_place_queue.py -v`
Expected: FAIL with `ModuleNotFoundError: app.services.google_map_scraper.place_queue`

- [ ] **Step 3: Implement the queue in `app/services/google_map_scraper/place_queue.py`**

Use plain list commands on `redis.Redis` (`rpush`, `lindex`, `lset`, `pipeline(transaction=True)` for `complete`, `lrange`, `exists`, `delete`, `expire`). Serialize with `json.dumps`/`json.loads`. Wrap each method body in `try: ... except RedisError as error: raise BufferError(f"...: {describe(error)}") from error` using `app.utils.errors.describe`.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pytest tests/services/google_map_scraper/test_place_queue.py -v`
Expected: PASS, 6 tests

- [ ] **Step 5: Commit**

```bash
git add app/services/google_map_scraper/place_queue.py tests/services/google_map_scraper/test_place_queue.py
git commit -m "feat: add Redis job queue for a place scrape run"
```

---

### Task 2: PlaceScraper enqueues and runs jobs through a listener

**Files:**
- Modify: `app/services/google_map_scraper/scraper.py` (`class PlaceScraper`, roughly lines 313-end)
- Test: `tests/services/google_map_scraper/test_scraper_queue.py`

**Interfaces:**
- Consumes: `PlaceJobQueue`, `PlaceJob`, `DoneKeyword` from Task 1.
- Produces:
  - `PlaceScraper.__init__(self, redis: Redis | None = None)`: default `connect(RedisConfig())`
  - `PlaceScraper._scrape_keyword(self, query, settings, name, bounding_box, queue) -> None`: builds a `PlaceJob` and calls `queue.enqueue`
  - `PlaceScraper._listen(self, query, known_keys, columns, queue) -> ScrapeResult`
  - `PlaceScraper._run_job(self, query, job, queue, known_keys, saved) -> DoneKeyword`: runs the job on a daemon thread, joins it, re-raises its exception
  - `PlaceScraper._scrape_single(...)` and `PlaceScraper._scrape_grid(...)`: the existing bodies moved here, reading and writing the `PlaceJob` fields instead of locals. Grid saves progress through `queue.save(job)` at the top of each cell iteration.

Behaviour to keep from the current code: the same prints, the same logger calls, the same exception handling per cell and per keyword, and the same stop rules (`blocked` or lost Chrome ends the run).

- [ ] **Step 1: Write the failing tests**

Tests use `fakeredis.FakeRedis(decode_responses=True)` and monkeypatch `run_scrape` and `get_geocode` as `tests/services/google_map_scraper/test_scraper.py` does.

```python
def test_a_crash_in_a_cell_resumes_at_that_cell(monkeypatch, redis):
    calls = []

    def fake_run(name, settings, **_):
        calls.append(settings.latitude)
        if len(calls) == 2:
            raise BufferError("redis down")
        return RunResult("id", ScrapeStatus.OK, "", [], False)

    monkeypatch.setattr(scraper_module, "run_scrape", fake_run)
    monkeypatch.setattr(scraper_module, "get_geocode", _grid_geocode)
    with pytest.raises(BufferError):
        PlaceScraper(redis).scrape(_query(grid=2, keywords=["cafes"]))

    calls.clear()
    monkeypatch.setattr(scraper_module, "run_scrape", lambda n, s, **_: (calls.append(s.latitude), RunResult("id", ScrapeStatus.OK, "", [], False))[1])
    PlaceScraper(redis).scrape(_query(grid=2, keywords=["cafes"]))
    assert len(calls) == 3  # cell 2 runs again, then cells 3 and 4


def test_finished_keywords_are_not_searched_again_on_resume(monkeypatch, redis):
    names = []

    def fake_run(name, settings, **_):
        names.append(settings.keywords[0])
        if settings.keywords[0] == "bakeries" and names.count("bakeries") == 1:
            raise BufferError("redis down")
        return RunResult("id", ScrapeStatus.OK, "", [], False)

    monkeypatch.setattr(scraper_module, "run_scrape", fake_run)
    monkeypatch.setattr(scraper_module, "get_geocode", lambda *a, **k: (1.0, 2.0))
    with pytest.raises(BufferError):
        PlaceScraper(redis).scrape(_query(keywords=["cafes", "bakeries"]))
    names.clear()
    monkeypatch.setattr(scraper_module, "run_scrape", fake_run)
    PlaceScraper(redis).scrape(_query(keywords=["cafes", "bakeries"]))
    assert names == ["bakeries"]


def test_a_rate_limit_ends_the_run_and_clears_the_queue(monkeypatch, redis):
    monkeypatch.setattr(scraper_module, "run_scrape", lambda n, s, **_: RunResult("id", ScrapeStatus.OK, "", [_row()], True))
    monkeypatch.setattr(scraper_module, "get_geocode", lambda *a, **k: (1.0, 2.0))
    result = PlaceScraper(redis).scrape(_query(keywords=["cafes", "bakeries"]))
    assert result.blocked
    assert not redis.keys("place_queue:*")


def test_a_run_with_one_job_per_keyword_returns_rows_in_keyword_order(monkeypatch, redis):
    monkeypatch.setattr(scraper_module, "run_scrape", lambda n, s, **_: RunResult("id", ScrapeStatus.OK, "", [_row(title=s.keywords[0])], False))
    monkeypatch.setattr(scraper_module, "get_geocode", lambda *a, **k: (1.0, 2.0))
    result = PlaceScraper(redis).scrape(_query(keywords=["cafes", "bakeries"]))
    assert [row["title"] for row in result.rows] == ["cafes", "bakeries"]
    assert not redis.keys("place_queue:*")
```

`_row`, `_query`, `_grid_geocode`, `_scrape_keyword` helpers: copy from `test_scraper.py`. Imports: `from app.utils`... follow that file's imports; add `import fakeredis` and `from app.services.buffer.client import BufferError`.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/services/google_map_scraper/test_scraper_queue.py -v`
Expected: FAIL, because `PlaceScraper` takes no `redis` argument.

- [ ] **Step 3: Implement the changes in `scraper.py`**

- `scrape()`: build `queue = PlaceJobQueue(self._redis, query, BUFFER_TTL_SECONDS)`. If `queue.exists()` is false, run the geocoding (moved into `_enqueue_run`, which returns a `ScrapeResult` for the early-exit cases, else `None`) and call `_scrape_keyword` per keyword. Then `return self._listen(query, known_keys, columns, queue)`.
- `_scrape_keyword`: compute `cells = build_grid_cells(bounding_box, query.grid) if query.grid else None` and enqueue a `PlaceJob` with `settings.model_dump(by_alias=True, mode="json")` and `list(bounding_box)` when set.
- `_listen`: loop while `queue.peek()` is not None. Print `▶ Keyword {n}/{total}: {keyword}` where `n = len(queue.done()) + 1`. Compute `saved` as the `kept` rows of `queue.done()`, and `known = known_keys | frozenset(keys of saved)` using `lead_keys`. Call `_run_job`, then `queue.complete(done)`. Break when `done["blocked"]`. The loop breaks after the stop record is stored, so `_listen` then calls `queue.clear()` and returns the `ScrapeResult` built from `queue.done()`. Clear the queue on normal return too.
- `_run_job`: thread target catches `BaseException` into a holder dict. Use `Thread(target=..., name=f"place-job-{keyword}", daemon=True)`, `start()`, `join()`, re-raise the captured error.
- `_scrape_grid`: restore `bounding_box` and `cells` from the job (tuples for `_bbox`). Start from `job["next_cell"]`. At the top of each loop iteration call `job.update(next_cell=index, rows=rows, kept=kept, finished=finished, skipped=skipped, cells=cells)` and `queue.save(job)`. Return a `DoneKeyword`.
- `_scrape_single`: same as the current single-scrape branch, with no progress saved. Returns a `DoneKeyword`.
- `self._salvage = saved + kept` in both paths, as today.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pytest tests/services/google_map_scraper/test_scraper_queue.py tests/services/google_map_scraper/test_scraper.py -v`
Expected: PASS. The existing `test_scraper.py` tests will fail until Task 3 updates them.

- [ ] **Step 5: Commit**

```bash
git add app/services/google_map_scraper/scraper.py tests/services/google_map_scraper/test_scraper_queue.py
git commit -m "feat: run place scrape keywords through a resumable queue"
```

---

### Task 3: Existing scraper tests use a fake Redis

**Files:**
- Modify: `tests/services/google_map_scraper/test_scraper.py`

**Interfaces:**
- Consumes: `PlaceScraper(redis)` from Task 2.

- [ ] **Step 1: Update the tests**

Add a fixture to the module:

```python
@pytest.fixture
def redis():
    return fakeredis.FakeRedis(decode_responses=True)
```

Change every `PlaceScraper().scrape(` call to `PlaceScraper(redis).scrape(`, and add a `redis` argument to each test that calls it. Test functions that build `PlaceScraper()` directly (`test_grid_stops_when_a_tiles_leads_stay_unsaved`, `test_each_keyword_is_its_own_scrape`, and others) are updated the same way.

- [ ] **Step 2: Run the tests to verify they pass**

Run: `pytest tests/services/google_map_scraper/ -v`
Expected: PASS

- [ ] **Step 3: Commit**

```bash
git add tests/services/google_map_scraper/test_scraper.py
git commit -m "test: give the place scraper tests a fake Redis"
```

---

### Task 4: Full verification

- [ ] **Step 1: Run the whole test suite**

Run: `pytest -q`
Expected: all tests pass, no new warnings from the scraper module.

- [ ] **Step 2: Update the code graph**

Run: `graphify update .`
Expected: command completes without error.

</content>
