# Parallel proxy tile queue Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `--proxies URL,URL,...` runs one city tile per proxy in parallel from one shared tile queue, and a rate-limited proxy only retires its own worker.

**Architecture:** One `ScrapeSession` per city run owns a pool of `ScrapeWorker` threads, one per proxy. The `PlaceScraper` coordinator fills free workers from a Redis tile queue, and handles results, dedupe, deep-scan sub-tiles, and re-queues. All workers share one Chrome `Browser` through `SharedBrowser`; each tile opens its own context via the existing `ProxyTab`.

**Tech Stack:** Python 3.12, Tortoise ORM (PostgreSQL), Redis, pytest, ruff.

**Spec:** `docs/superpowers/specs/2026-11-15-parallel-proxy-tile-queue-design.md` (read the Amendment section too). Earlier design: `docs/superpowers/specs/2026-11-15-place-scrape-queue-design.md`.

## Global Constraints

- A tile's retries never run on another worker's proxy: each tile's settings carry exactly its own proxy (`proxies=(own,)`) or none.
- At most one tile runs per proxy at a time; no proxy means one worker with no proxy.
- A proxy failure or block retires its worker for the run; a tile that fails there is re-queued at most once per proxy, then counted as skipped.
- A lost Chrome connection or a crash keeps the Redis queue; a normal finish or every worker retiring deletes it.
- With no `--proxies`, output and behaviour match today's sequential run.
- Use the ruff and pytest settings already in `pyproject.toml`; keep the existing code style.

## Review Focus

1. `--proxies` with spaces, a trailing comma or a repeated proxy: blanks dropped, repeats collapsed, empty value rejected. (Task 1)
2. A tile fails on its worker's proxy: the tile is re-queued and never runs on another worker's proxy at the same time. (Task 7)
3. The browser crashes while several tiles run: the shared Browser restarts once and the other tiles' contexts are not reset by sibling workers. (Task 3)
4. Ctrl-C with tiles in flight: in-flight tiles finish, the queue is kept, and `salvage_rows` keeps all kept leads. (Task 7)
5. Resume after a crash: in-flight tile scrapes are resumed by id and finished tiles are never searched again. (Task 7)

---

### Task 1: CLI `--proxies` comma list

**Files:**
- Modify: `cli/google_map_scrape_cli.py` (`build_parser`, the `--proxy` argument; `FLAG_NAMES`; add `proxy_names`)
- Test: `tests/cli/test_google_map_scrape_cli.py`

**Interfaces:**
- Consumes: `comma_list(text, label)` already in the CLI.
- Produces: `proxy_names(text: str) -> list[str]`, used as the argparse `type` for `--proxies`. The `CliQuery.proxies` field is unchanged; `PlaceQuery` still validates each URL with `parse_proxy_urls`.

- [ ] **Step 1: Write the failing tests**

```python
def test_proxies_split_on_commas_and_drop_blanks_and_repeats():
    assert proxy_names(" http://a:1 , http://b:2,,http://a:1 ") == ["http://a:1", "http://b:2"]

def test_empty_proxies_value_is_rejected():
    with pytest.raises(argparse.ArgumentTypeError):
        proxy_names(" , ")

def test_dry_run_counts_comma_separated_proxies(capsys):
    main_with_args(["--keywords", "cafe", "--city", "Austin", "--country", "us",
                    "--proxies", "http://a:1,http://b:2", "--dry-run"])
    assert "Proxies: 2" in capsys.readouterr().out
```

Use the existing helper for running the CLI in that test file; if none exists, call `build_parser().parse_args(...)` and `CliQuery.model_validate`.

- [ ] **Step 2: Run the tests and confirm they fail**

Run: `pytest tests/cli/test_google_map_scrape_cli.py -k "proxies" -v`
Expected: FAIL with `ImportError` for `proxy_names`.

- [ ] **Step 3: Implement `proxy_names` and the `--proxies` argument**

Add `proxy_names` next to `keyword_names`, using `comma_list(text, "proxy")`. Replace the `--proxy` argument (`action="append"`) with `--proxies`, `type=proxy_names`, `metavar="URL,..."`, and keep `dest="proxies"`. Update the help text to describe comma separation. Set `FLAG_NAMES = {"proxies": "--proxies"}`.

- [ ] **Step 4: Run the tests and confirm they pass**

Run: `pytest tests/cli/test_google_map_scrape_cli.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add cli/google_map_scrape_cli.py tests/cli/test_google_map_scrape_cli.py
git commit -m "feat: accept comma-separated --proxies"
```

---

### Task 2: Atomic claim of one scrape by id

**Files:**
- Modify: `app/repositories/scrape_repository.py` (add `claim_by_id` next to `claim_oldest_pending`, line 62)
- Modify: `app/services/google_map_scraper/scrape/scrape_database.py` (add `claim(scrape_id)` next to `claim_next_pending`, line 65)
- Test: `tests/services/google_map_scraper/scrape/test_scrape_database.py`

**Interfaces:**
- Consumes: the `ScrapeStatus.PENDING` / `WORKING` constants.
- Produces: `ScrapeDatabase.claim(scrape_id: str) -> ScrapeRecord | None`. Returns the record with status `WORKING` if it was `PENDING`, else `None`.

- [ ] **Step 1: Write the failing test**

```python
def test_claim_takes_one_pending_scrape_by_id(database):
    scrape_id = database.create("tile", SETTINGS)
    first = database.claim(scrape_id)
    assert first is not None and first["Status"] == ScrapeStatus.WORKING
    assert database.claim(scrape_id) is None
```

Use the database fixture already defined in that test file (or the `conftest.py` beside it).

- [ ] **Step 2: Run it and confirm it fails**

Run: `pytest tests/services/google_map_scraper/scrape/test_scrape_database.py -k claim_takes -v`
Expected: FAIL with `AttributeError: 'ScrapeDatabase' object has no attribute 'claim'`.

- [ ] **Step 3: Implement**

In the repository, filter `id=scrape_id, status=PENDING`, add `select_for_update()`, set `WORKING`, save, and return `_to_record`. Return `None` when nothing matches. Mirror the body of `claim_oldest_pending`, inside `in_transaction()`. Add the `ScrapeDatabase.claim` wrapper with `self._call("claiming a scrape", ...)`.

- [ ] **Step 4: Run it and confirm it passes**

Run: `pytest tests/services/google_map_scraper/scrape/test_scrape_database.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/repositories/scrape_repository.py app/services/google_map_scraper/scrape/scrape_database.py tests/services/google_map_scraper/scrape/test_scrape_database.py
git commit -m "feat: claim a pending scrape by id"
```

---

### Task 3: Shared Chrome browser for all workers

**Files:**
- Create: `app/services/browser/shared_browser.py` (class `SharedBrowser`)
- Modify: `app/services/browser/browser_session.py` (`BrowserSession`: optional `shared` argument)
- Modify: `app/services/browser/__init__.py` (export `SharedBrowser`)
- Test: `tests/services/browser/test_shared_browser.py`

**Interfaces:**
- Produces:
  - `SharedBrowser(locale: str, headless: bool)`
    - `current() -> Browser | None`
    - `ensure() -> Browser`: starts the Browser under a lock if none is running; raises `BrowserError` if it cannot start.
    - `restart_if_dead(seen: Browser | None) -> Browser`: under the lock, if the current browser is `seen` (or is dead), starts a new one. If another worker has already replaced it, returns the current one. This makes sure one crash causes one restart.
    - `close() -> None`.
  - `BrowserSession(locale, headless, shared: SharedBrowser | None = None)`. When `shared` is set:
    - `start()` calls `shared.ensure()`; `browser` returns `shared.current()`; `is_dead` uses the same check as today.
    - `restart()` calls `shared.restart_if_dead(seen=self.browser)`.
    - `kill()` and `close()` are no-ops. A tile must not kill the browser its siblings use.
    - `open_tab` is unchanged.

- [ ] **Step 1: Write the failing tests** (use a fake `Browser` class with `cdp.closed` and `close()`, monkeypatched into `shared_browser.Browser`)

```python
def test_shared_browser_starts_one_browser_for_two_sessions(fake_browser_cls):
    shared = SharedBrowser("en", True)
    a, b = BrowserSession("en", True, shared=shared), BrowserSession("en", True, shared=shared)
    a.start(); b.start()
    assert fake_browser_cls.created == 1
    assert a.browser is b.browser

def test_closing_one_attached_session_keeps_the_browser_for_the_other(fake_browser_cls):
    shared = SharedBrowser("en", True)
    a, b = BrowserSession("en", True, shared=shared), BrowserSession("en", True, shared=shared)
    a.start(); b.start()
    a.close(); a.kill()
    assert b.browser is not None and not b.browser.closed

def test_restart_after_a_crash_starts_one_new_browser(fake_browser_cls):
    shared = SharedBrowser("en", True)
    a, b = BrowserSession("en", True, shared=shared), BrowserSession("en", True, shared=shared)
    a.start(); b.start()
    fake_browser_cls.last.cdp.closed = True
    a.restart(); b.restart()
    assert fake_browser_cls.created == 2
```

- [ ] **Step 2: Run them and confirm they fail**

Run: `pytest tests/services/browser/test_shared_browser.py -v`
Expected: FAIL with `ImportError` for `SharedBrowser`.

- [ ] **Step 3: Implement** `SharedBrowser` with a `threading.Lock` and the methods above, and the `shared` branch in `BrowserSession`. Keep the non-shared path exactly as it is now.

- [ ] **Step 4: Run them and confirm they pass; also run the existing browser tests**

Run: `pytest tests/services/browser -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/services/browser tests/services/browser/test_shared_browser.py
git commit -m "feat: share one Chrome between scrape workers"
```

---

### Task 4: `ScrapeRun` takes an attached browser

**Files:**
- Modify: `app/services/google_map_scraper/scrape/scrape_run.py` (`ScrapeRun.__init__`, line 60 area; `_on_watchdog`, line ~340)
- Test: `tests/services/google_map_scraper/scrape/test_scrape_run_buffer.py`

**Interfaces:**
- Consumes: `BrowserSession(..., shared=...)` from Task 3.
- Produces: `ScrapeRun(database, scrape, headless, redis, known_keys=frozenset(), browser: BrowserSession | None = None)`. When `browser` is None, the run builds its own `BrowserSession` as today. Otherwise it uses the given one.

- [ ] **Step 1: Write the failing test**

```python
def test_attached_run_does_not_kill_the_shared_browser(fake_browser_session, scrape_record, redis):
    run = ScrapeRun(database=FakeDatabase(), scrape=scrape_record, headless=True, redis=redis,
                    browser=fake_browser_session)
    run._on_watchdog()
    assert fake_browser_session.kill_calls == 0
    assert run._stop.is_set()
```

`fake_browser_session` is a stub with `kill_calls` and `close_calls` counters; the test file's fixtures already build a `ScrapeRun` (see `test_scrape_run_buffer.py`), follow those.

- [ ] **Step 2: Run it and confirm it fails**

Run: `pytest tests/services/google_map_scraper/scrape/test_scrape_run_buffer.py -k attached -v`
Expected: FAIL with `TypeError` (unexpected keyword `browser`).

- [ ] **Step 3: Implement**

Add the `browser` parameter. Store `self._browser = browser or BrowserSession(...)`. In `_on_watchdog`, keep setting `timed_out` and `_stop`; the kill is a no-op for attached sessions, so the tile stops at its next check. Keep `execute`'s `finally: self._browser.close()`, which is a no-op for attached sessions. Add a comment at the watchdog explaining that a stuck CDP call on a shared browser is only ended by its timeout. This is a known limit from the amendment.

- [ ] **Step 4: Run and confirm it passes**

Run: `pytest tests/services/google_map_scraper/scrape -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/services/google_map_scraper/scrape/scrape_run.py tests/services/google_map_scraper/scrape/test_scrape_run_buffer.py
git commit -m "feat: let a scrape run use an attached browser"
```

---

### Task 5: `ScrapeWorker` assigned mode

**Files:**
- Modify: `app/services/google_map_scraper/scrape/scrape_worker.py` (`ScrapeWorker.__init__`, `run`, `_execute`)
- Test: `tests/services/google_map_scraper/scrape/test_scrape_run_buffer.py` (or a new `test_scrape_worker_pool.py` beside it)

**Interfaces:**
- Consumes: `ScrapeDatabase.claim(scrape_id)` (Task 2), `SharedBrowser` and `BrowserSession(shared=...)` (Task 3), `ScrapeRun(browser=...)` (Task 4).
- Produces: `ScrapeWorker(database, redis, headless=True, known_keys=frozenset(), on_finished=None, assignments: queue.Queue[str] | None = None, browser: BrowserSession | None = None, proxy_slot: int = 0)`.
  - `assignments is None`: today's behaviour (`claim_next_pending`).
  - otherwise, `run()` takes scrape ids from `assignments`, claims each with `database.claim`, and runs it with the given `browser`. A `None` from `claim` skips the id. `_wait` uses `assignments.get(timeout=_IDLE_POLL_SECONDS)`, and the loop still exits on `shutdown`.
  - `shutdown` and `cancel` keep their current meaning, one worker at a time.

- [ ] **Step 1: Write the failing tests**

```python
def test_assigned_worker_runs_only_the_ids_it_is_given(fake_db, fake_run_factory):
    ids = queue.Queue(); ids.put("tile-1"); ids.put("tile-2")
    worker = ScrapeWorker(fake_db, redis=None, assignments=ids, browser=None)
    worker.start(); wait_until(lambda: len(fake_db.claimed) == 2); worker.shutdown(wait=2)
    assert fake_db.claimed == ["tile-1", "tile-2"]
    assert fake_db.claim_next_calls == 0

def test_assigned_worker_skips_an_id_that_is_no_longer_pending(fake_db):
    fake_db.refuse = {"tile-1"}
    ids = queue.Queue(); ids.put("tile-1")
    worker = ScrapeWorker(fake_db, redis=None, assignments=ids)
    worker.start(); wait_until(lambda: "tile-1" in fake_db.refused_seen); worker.shutdown(wait=2)
    assert fake_db.ran == []
```

`fake_db` / `fake_run_factory` are small stubs in the test file. Replace `ScrapeRun` in the worker with a factory so the tests do not run Chrome.

- [ ] **Step 2: Run and confirm they fail**

Run: `pytest tests/services/google_map_scraper/scrape -k assigned -v`
Expected: FAIL with `TypeError` (unexpected keyword `assignments`).

- [ ] **Step 3: Implement**

Add the new parameters. In `run()`, branch on `self._assignments`. In assigned mode, get an id with a timeout, call `self._database.claim(scrape_id)`, and if the record is `None` skip it. Otherwise call `_execute(record)`. `_execute` builds `ScrapeRun(..., browser=self._browser)` when a browser is given. Keep `_current` per worker. `shutdown` and `cancel` are unchanged.

- [ ] **Step 4: Run and confirm they pass; run the scrape tests**

Run: `pytest tests/services/google_map_scraper/scrape -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/services/google_map_scraper/scrape/scrape_worker.py tests/services/google_map_scraper/scrape
git commit -m "feat: scrape worker can run ids assigned to it"
```

---

### Task 6: `ScrapeSession` runs a pool of workers

**Files:**
- Modify: `app/services/google_map_scraper/scraper.py` (`ScrapeSession`, lines ~66-260: `__init__`, `run`, `stop`, `_finish`)
- Test: `tests/services/google_map_scraper/test_scraper.py`

**Interfaces:**
- Consumes: `ScrapeWorker(..., assignments=..., browser=..., proxy_slot=...)` (Task 5); `SharedBrowser` (Task 3).
- Produces:
  - `ScrapeSession(config, redis, postgres, known_keys, slots: int)`. It creates `slots` workers, one per proxy slot, each with its own `queue.Queue` of ids, all sharing one `SharedBrowser`.
  - `start() -> list[str]`: resumes interrupted scrapes once (as `_resume_interrupted` does now) and returns the carried ids. Starts the Flusher and the workers.
  - `submit(scrape_id: str, slot: int) -> None`: gives a tile id to a free slot.
  - `next_finished(timeout: float) -> FinishedTile | None`: returns a tile's outcome and rows when one finishes. `FinishedTile = (scrape_id, RunOutcome, rows, slot)`, and the slot is included so the coordinator can free it.
  - `finish(scrape_id: str) -> RunResult | None`: runs today's per-scrape finish logic (`_finish` for one id) and returns the result, or `None` if the tile's leads are not saved. Raises `UnsavedLeadsError` / `ScrapeDeletedError` as today.
  - `stop()` stops every worker and the Flusher, and discards deleted scrapes, as today.

- [ ] **Step 1: Write the failing test** (fake workers and fake database, no Chrome)

```python
def test_session_runs_each_submitted_tile_on_its_slot(fake_session_env):
    session = fake_session_env.session(slots=2)
    session.start()
    session.submit("tile-a", 0); session.submit("tile-b", 1)
    finished = {session.next_finished(timeout=2)[0], session.next_finished(timeout=2)[0]}
    assert finished == {"tile-a", "tile-b"}
```

- [ ] **Step 2: Run and confirm it fails**

Run: `pytest tests/services/google_map_scraper/test_scraper.py -k runs_each_submitted -v`
Expected: FAIL with `TypeError` (unexpected keyword `slots`).

- [ ] **Step 3: Implement**

Split `_wait_for_scrape` and `_finish` so the per-scrape finish logic is reusable: `_finish` keeps its loop over `carried` and `scrape_id` and calls a new `_finish_one(finished_id)`. Keep the `RunResult` semantics for the single-run path. `next_finished` reads from one shared `queue.Queue` that the workers' `on_finished` callback fills, with the slot.

- [ ] **Step 4: Run and confirm it passes**

Run: `pytest tests/services/google_map_scraper/test_scraper.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/services/google_map_scraper/scraper.py tests/services/google_map_scraper/test_scraper.py
git commit -m "feat: scrape session runs a worker per proxy slot"
```

---

### Task 7: Tile queue and coordinator in `PlaceScraper`

**Files:**
- Modify: `app/services/google_map_scraper/place_queue.py` (replace `PlaceJob` with `PlaceTile`; the queue keeps pending, in-flight and done lists)
- Modify: `app/services/google_map_scraper/scraper.py` (`PlaceScraper.scrape`, `_enqueue_run`, `_listen`; `_scrape_single` and `_scrape_grid` are replaced)
- Test: `tests/services/google_map_scraper/test_place_queue.py`, `tests/services/google_map_scraper/test_scraper_queue.py`

**Interfaces:**
- Consumes: `ScrapeSession` (Task 6), `ScrapeDatabase.create` (existing), `build_grid_cells` (existing), `finalize_rows` and `dedupe_rows` (existing).
- Produces:
  - `PlaceTile` (TypedDict): `keyword`, `name`, `settings: SettingsData` (already with this tile's own proxy or `()`), `bounding_box`, `cell: GridCell | None`, `attempts: int`, `scrape_id: str | None`.
  - `PlaceTileQueue(redis, query, ttl)` with:
    - `exists()`, `clear()`
    - `push(tile)` appends to pending; `push_front(tile)` puts a re-queued tile first
    - `take() -> PlaceTile | None`: moves the first pending tile to in-flight in one Redis transaction
    - `mark_scrape(tile_id, scrape_id)` stores the scrape id on an in-flight tile
    - `finish(tile_id, done: DoneTile)`: removes it from in-flight and appends to done
    - `in_flight() -> list[PlaceTile]`, `pending() -> list[PlaceTile]`, `done() -> list[DoneTile]`
    - `DoneTile` has `kept`, `finished`, `skipped`, `blocked`, `rows` (the deduped rows, so dedupe survives a resume).
- Coordinator (in `PlaceScraper`):
  - Start: enqueue every keyword's tiles (grid cells or one whole-city tile per keyword), in keyword order, then cell order. On resume, requeue in-flight tiles by submitting their stored `scrape_id` to the session. The session resumes those scrapes through `_resume_interrupted`.
  - Loop: fill every free slot with `take()`, create the scrape row for the tile (`ScrapeDatabase.create`), store its id, and `submit` it. On each `next_finished`:
    - the rows go through `dedupe_rows` against `known_keys` plus rows already done, and `finalize_rows` (per tile bounding box, as today);
    - `result.blocked` or a proxy failure: the slot retires. The tile goes back with `push_front` if `attempts < number of proxies`, else it counts as skipped. The attempt counter lives in `attempts`;
    - deep scan: if the tile's raw row count is at least `SATURATED`, add its four sub-tiles with `push`;
    - the tile is `finish`ed in the queue, and `ScrapeSession.finish(scrape_id)` gives its `RunResult`.
  - Stop: when every slot has retired, or a lost Chrome connection stops the run, the run ends. The queue is cleared only for a normal finish or when every slot retired on a rate limit or proxy failure. Otherwise it is kept.
  - `salvage_rows` is updated after every finished tile. Ctrl-C sets `_cancel`, so no new tile is taken; in-flight tiles finish.

- [ ] **Step 1: Write the failing tests** (fake session, fake database, in-memory Redis from the existing test helpers)

```python
def test_rate_limited_tile_is_requeued_and_runs_on_a_healthy_slot(env):
    env.session.script = {"tile-1": blocked_on_slot(0), "tile-1@1": done_rows(3)}
    result = env.scraper.scrape(env.query_with_proxies("http://a:1", "http://b:2"))
    assert env.slots_used_for("tile-1") == [0, 1]
    assert result.finished == 1 and result.blocked is False

def test_a_tile_never_runs_on_another_slots_proxy(env):
    env.session.script = {"tile-1": proxy_failure_on_slot(0)}
    env.scraper.scrape(env.query_with_proxies("http://a:1", "http://b:2"))
    assert all(tile.settings["proxies"] in ((), ("http://a:1",), ("http://b:2",)) for tile in env.submitted)
    assert all(len(tile.settings["proxies"]) <= 1 for tile in env.submitted)

def test_tile_is_skipped_after_one_attempt_per_proxy(env):
    env.session.script = {"tile-1": blocked_everywhere()}
    result = env.scraper.scrape(env.query_with_proxies("http://a:1", "http://b:2"))
    assert result.skipped >= 1

def test_interrupt_keeps_in_flight_tiles_and_salvage(env):
    env.session.script = {"tile-1": done_rows(2), "tile-2": interrupt_on_run()}
    with pytest.raises(KeyboardInterrupt):
        env.scraper.scrape(env.query_with_proxies("http://a:1", "http://b:2"))
    assert len(env.scraper.salvage_rows) == 2
    assert env.queue.exists()

def test_resume_submits_in_flight_scrape_ids_and_does_not_rerun_done_tiles(env):
    env.queue.push(tile_a); env.queue.take(); env.queue.mark_scrape(tile_a["ID"], "scrape-a")
    env.queue.push(tile_b)
    env.scraper.scrape(env.query)
    assert "scrape-a" in env.session.resubmitted
    assert env.session.searched_tiles == [tile_a["ID"], tile_b["ID"]]
```

Adapt helper names to the existing test files (`test_scraper_queue.py` already has fake sessions and an in-memory Redis); keep the assertions.

- [ ] **Step 2: Run and confirm they fail**

Run: `pytest tests/services/google_map_scraper/test_place_queue.py tests/services/google_map_scraper/test_scraper_queue.py -v`
Expected: FAIL (new `PlaceTileQueue` and coordinator not yet in place).

- [ ] **Step 3: Implement** `PlaceTile`, `DoneTile` and `PlaceTileQueue` in `place_queue.py`, with `take` as one Lua script or a `MULTI` block, so one tile is never taken twice. In `scraper.py`, replace `_listen`, `_run_job`, `_scrape_single` and `_scrape_grid` with the coordinator described above. Keep `scrape()`'s signature, `salvage_rows`, and the `ScrapeResult` it returns.

- [ ] **Step 4: Run and confirm they pass**

Run: `pytest tests/services/google_map_scraper -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/services/google_map_scraper/place_queue.py app/services/google_map_scraper/scraper.py tests/services/google_map_scraper
git commit -m "feat: run city tiles from one queue across proxy workers"
```

---

### Task 8: Wire the proxies and run the whole suite

**Files:**
- Modify: `app/services/google_map_scraper/scraper.py` (`PlaceScraper.__init__`/`scrape` read `query.proxies`; `slots = max(1, len(query.proxies))`)
- Modify: `app/services/google_map_scraper/place_query.py` only if `PlaceQuery` needs to expose the per-slot proxy (it already carries `proxies`)

**Interfaces:**
- Consumes: everything above.
- Produces: `slots = max(1, len(query.proxies))`, and each tile's `settings.proxies` is `(query.proxies[slot],)`, or `()` when there are no proxies.

- [ ] **Step 1: Write the failing test**

```python
def test_no_proxies_means_one_slot_without_proxy(env):
    env.scraper.scrape(env.query_without_proxies())
    assert env.session.slot_count == 1
    assert all(tile.settings["proxies"] == () for tile in env.submitted)
```

- [ ] **Step 2: Run and confirm it fails**, then implement, then run again.

Run: `pytest tests/services/google_map_scraper/test_scraper_queue.py -k no_proxies -v`
Expected: FAIL, then PASS after the change.

- [ ] **Step 3: Full verification**

Run: `pytest -q`
Run: `ruff check . && ruff format --check .`
Run: `python -m cli.google_map_scrape_cli --keywords "cafe" --city Austin --country us --proxies "http://a:1,http://b:2" --dry-run`
Expected: all tests pass, ruff is clean, and the dry run shows `Proxies: 2`.

- [ ] **Step 4: Update the graph and commit**

Run: `graphify update .`

```bash
git add app/services/google_map_scraper/scraper.py graphify-out
git commit -m "feat: wire --proxies into parallel tile workers"
```
