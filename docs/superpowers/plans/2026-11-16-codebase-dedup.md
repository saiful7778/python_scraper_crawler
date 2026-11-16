# Deduplicated Helpers Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Collapse six duplicated helper families into one canonical implementation each, with all callers and tests updated.

**Architecture:** Each family gets a single owner module/function: time in `app/utils/utils.py`, host parsing in `app/utils/url_utils.py`, header lookup in `app/helpers/http_fetcher/responses.py`, Postgres plumbing in a new `app/services/postgres.py`, the global counter in `app/repositories/base.py`, and Redis list reads in a new `app/services/buffer/redis_lists.py`. Duplicate copies are deleted, never wrapped.

**Tech Stack:** Python 3.12, tortoise-orm, redis, pytest, ruff, pyright.

**Spec:** `docs/superpowers/specs/2026-11-16-codebase-dedup-design.md`

## Global Constraints

- Python 3.12; no new dependencies.
- `ruff check .` must stay at the 4 pre-existing errors (no new ones).
- Full suite baseline: 918 passed.
- Do not change Redis keys, Postgres tables, or row shapes beyond timestamp precision.
- After all tasks: `graphify update .`.

## Review Focus

- Padded host input (`"  https://www.Example.com/x  "`) must become `example.com`, never `"  "`; a wrong guess writes a garbage value into the unique `leads.website_domain` column.
- A response with no `Content-Type` must stay not-crawlable now that the helper returns `""` instead of `None`.
- Timestamps written before this change are second-precision and must still parse with `datetime.fromisoformat`.
- A dropped Postgres connection during a save must reconnect once and retry, not lose the row (existing flusher tests pin this).
- Two repository instances must not hand out the same global counter.

---

### Task 1: Canonical UTC time helpers

**Files:**
- Modify: `app/utils/utils.py`
- Modify: `app/services/buffer/crawl_buffer.py`
- Modify: `app/http/resources/lead_resource.py`
- Modify: `app/repositories/base.py`
- Test: `tests/utils/test_utils.py`

**Interfaces:**
- Produces: `utc_now() -> str` (UTC ISO 8601 with microseconds) and `isoformat_or_none(value: datetime | None) -> str | None`.
- Removes: `app.utils.utils.log`, `BaseRepository.now`, `crawl_buffer._now`, `Resource.iso`.

- [ ] **Step 1: Rewrite `tests/utils/test_utils.py`**

```python
import re
from datetime import UTC, datetime, timedelta

from app.services.buffer.crawl_buffer import _restore
from app.utils.utils import isoformat_or_none, utc_now


def test_utc_now_is_iso_utc_timestamp_with_microseconds() -> None:
    value = utc_now()
    parsed = datetime.fromisoformat(value)
    assert parsed.utcoffset() == timedelta(0)
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}\+00:00", value)


def test_isoformat_or_none_keeps_none() -> None:
    assert isoformat_or_none(None) is None


def test_isoformat_or_none_formats_a_datetime() -> None:
    moment = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
    assert isoformat_or_none(moment) == "2026-01-02T03:04:05+00:00"


def test_restore_still_reads_second_precision_timestamps() -> None:
    row = _restore({"created_at": "2026-01-02T03:04:05+00:00", "id": "x"})
    assert row["created_at"] == datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
```

- [ ] **Step 2: Run it and confirm it fails**

Run: `uv run pytest tests/utils/test_utils.py -q`
Expected: FAIL — `isoformat_or_none` does not exist and the regex does not match seconds-only output.

- [ ] **Step 3: Implement the canonical helpers**

`app/utils/utils.py` becomes only:

```python
from datetime import UTC, datetime


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def isoformat_or_none(value: datetime | None) -> str | None:
    return None if value is None else value.isoformat()
```

Delete `log` and the `sys` import.

- [ ] **Step 4: Point the callers at the canonical helpers**

- `app/services/buffer/crawl_buffer.py`: delete `_now`; import `utc_now` from `app.utils.utils`; replace every `_now()` call with `utc_now()`; keep `datetime` imported for `_restore` but drop `UTC`.
- `app/http/resources/lead_resource.py`: import `isoformat_or_none`; replace the five `self.iso(...)` calls with `isoformat_or_none(...)`; delete the `iso` staticmethod and the `datetime` import if nothing else uses it.
- `app/repositories/base.py`: delete the `now` staticmethod and its `utc_now` import.

- [ ] **Step 5: Run the affected tests**

Run: `uv run pytest tests/utils tests/repositories tests/services/buffer -q`
Expected: PASS (the old `log` tests are gone by design).

- [ ] **Step 6: Commit**

```bash
git add app/utils/utils.py app/services/buffer/crawl_buffer.py app/http/resources/lead_resource.py app/repositories/base.py tests/utils/test_utils.py
git commit -m "refactor: one canonical UTC timestamp helper"
```

---

### Task 2: Canonical host extraction

**Files:**
- Modify: `app/utils/url_utils.py:58-65`
- Modify: `app/repositories/lead_repository.py:20-28`
- Modify: `app/services/crawler/extractors/seo.py:24-26,72,82`
- Modify: `app/services/buffer/crawl_sink.py:64`
- Test: `tests/utils/test_url_utils.py`, `tests/repositories/test_lead_repository_website.py`

**Interfaces:**
- Produces: `extract_site_host(url: str) -> str` — strips whitespace first; returns `""` when there is no host.
- Removes: `seo._host`, `lead_repository.website_domain`'s private parsing (it becomes a thin wrapper returning `str | None`).

- [ ] **Step 1: Add padded-input tests**

In `tests/utils/test_url_utils.py`, add to the `extract_site_host` parameter list:

```python
("  https://www.Example.com/x  ", "example.com"),
("   ", ""),
("  www.example.com  ", "example.com"),
```

Add one `same_scope` test:

```python
def test_same_scope_ignores_surrounding_whitespace() -> None:
    assert same_scope("  https://www.example.com/x  ", "example.com") is True
```

In `tests/repositories/test_lead_repository_website.py` add:

```python
def test_website_domain_strips_and_drops_www() -> None:
    assert website_domain("  https://www.Example.com/x  ") == "example.com"
    assert website_domain("   ") is None
```

- [ ] **Step 2: Run them and confirm they fail**

Run: `uv run pytest tests/utils/test_url_utils.py tests/repositories/test_lead_repository_website.py -q`
Expected: FAIL on the padded host cases.

- [ ] **Step 3: Make `extract_site_host` canonical**

In `app/utils/url_utils.py`, start the function with `text = url.strip()` and pass `text` to `_ensure_scheme`:

```python
def extract_site_host(url: str) -> str:
    """Return the lower-case host of ``url`` without a leading ``www.``."""
    try:
        host = urllib.parse.urlsplit(_ensure_scheme(url.strip())).hostname
    except ValueError as error:
        logger.debug("no host in %r: %s", url, describe(error))
        return ""
    return (host or "").removeprefix("www.")
```

- [ ] **Step 4: Replace the three duplicate parsers**

- `app/repositories/lead_repository.py`: `website_domain` becomes `return extract_site_host(website) or None`; import `extract_site_host` and drop `urlsplit` if unused elsewhere in the file.
- `app/services/crawler/extractors/seo.py`: delete `_host` and its `urlsplit` import; import `extract_site_host`; replace `_host(page_url)` and `_host(target)` with `extract_site_host(...)`.
- `app/services/buffer/crawl_sink.py`: `domain = extract_site_host(snapshot.website["host"])`.

- [ ] **Step 5: Run the affected tests**

Run: `uv run pytest tests/utils/test_url_utils.py tests/repositories/test_lead_repository_website.py tests/services/crawler/extractors tests/services/buffer/test_crawl_sink.py -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add app/utils/url_utils.py app/repositories/lead_repository.py app/services/crawler/extractors/seo.py app/services/buffer/crawl_sink.py tests/utils/test_url_utils.py tests/repositories/test_lead_repository_website.py
git commit -m "refactor: one canonical site-host helper"
```

---

### Task 3: Canonical response-header lookup

**Files:**
- Modify: `app/helpers/http_fetcher/responses.py`
- Modify: `app/services/crawler/pipeline/crawlability.py:36,47-53`
- Modify: `app/services/crawler/site/crawl.py:57-62,509,542,560`
- Test: create `tests/helpers/http_fetcher/test_responses.py`

**Interfaces:**
- Produces: `header_value(headers: Mapping[str, str], name: str) -> str` — case-insensitive, `""` when absent.
- Removes: both local `_header` functions.

- [ ] **Step 1: Write the failing test**

Create `tests/helpers/http_fetcher/test_responses.py`:

```python
from app.helpers.http_fetcher.responses import header_value


def test_header_value_is_case_insensitive() -> None:
    headers = {"Content-Type": "text/html"}
    assert header_value(headers, "content-type") == "text/html"
    assert header_value(headers, "CONTENT-TYPE") == "text/html"


def test_header_value_is_empty_when_absent() -> None:
    assert header_value({"server": "x"}, "content-type") == ""
```

- [ ] **Step 2: Run it and confirm it fails**

Run: `uv run pytest tests/helpers/http_fetcher/test_responses.py -q`
Expected: FAIL — `ImportError: cannot import name 'header_value'`.

- [ ] **Step 3: Implement `header_value` in `responses.py`**

```python
def header_value(headers: Mapping[str, str], name: str) -> str:
    """The value of a header by case-insensitive name; ``""`` when it is absent."""
    wanted = name.lower()
    for key, value in headers.items():
        if key.lower() == wanted:
            return value
    return ""
```

- [ ] **Step 4: Delete both local copies and use it**

- `crawlability.py`: `content_type = header_value(result.headers, "content-type")`; delete `_header`.
- `site/crawl.py`: `header_value(result.headers, "content-type")` at the three call sites; delete `_header`.

- [ ] **Step 5: Run the affected tests**

Run: `uv run pytest tests/helpers/http_fetcher/test_responses.py tests/services/crawler -q`
Expected: PASS, including the existing "not crawlable without content type" cases.

- [ ] **Step 6: Commit**

```bash
git add app/helpers/http_fetcher/responses.py app/services/crawler/pipeline/crawlability.py app/services/crawler/site/crawl.py tests/helpers/http_fetcher/test_responses.py
git commit -m "refactor: one canonical response-header lookup"
```

---

### Task 4: Shared Postgres connection plumbing

**Files:**
- Create: `app/services/postgres.py`
- Modify: `app/services/buffer/flusher.py:10-11,29-59,119,252-275,295-370`
- Modify: `app/services/google_map_scraper/scrape/scrape_database.py:14,42,140-205`
- Modify: `app/repositories/google_map_profile_repository.py:5,8,28-29`
- Test: `tests/services/buffer/test_flusher.py`

**Interfaces:**
- Produces: `app.services.postgres` exporting `CONNECTION`, `CONNECTION_LOST`, `context_config(config, connection=CONNECTION) -> dict`, `async check_connection(context) -> None`, and `TortoiseConnection`.
- `TortoiseConnection(config)`: `async open() -> TortoiseContext`, `async ensure() -> TortoiseContext`, `async drop() -> None`, `async close() -> None`, property `context -> TortoiseContext | None`.
- Removes: `Flusher._open`, `Flusher._ensure_connection`, `Flusher._drop_context`, `ScrapeDatabase._open`, `ScrapeDatabase._ensure_connection`, `ScrapeDatabase._drop_context`, and the duplicate `CONNECTION_LOST` in the repository.

- [ ] **Step 1: Update the flusher test to the new shape (failing first)**

In `tests/services/buffer/test_flusher.py`, replace each `flusher._context` with `flusher._connection.context` (lines 347, 354, 367, 372, 385, 392-395). Run:

`uv run pytest tests/services/buffer/test_flusher.py -q`
Expected: FAIL — `Flusher` has no `_connection`.

- [ ] **Step 2: Create `app/services/postgres.py`**

Move `CONNECTION`, `CONNECTION_LOST`, `context_config`, and `check_connection` verbatim from `flusher.py`. Add:

```python
class TortoiseConnection:
    """One long-lived Tortoise context, opened on first use and reopened after a loss."""

    def __init__(self, config: TortoiseConfig):
        self._config = config
        self._context: TortoiseContext | None = None

    @property
    def context(self) -> TortoiseContext | None:
        return self._context

    async def open(self) -> TortoiseContext: ...
    async def ensure(self) -> TortoiseContext: ...
    async def drop(self) -> None: ...
    async def close(self) -> None: ...
```

Bodies are the existing `_open`/`_ensure_connection`/`_drop_context` code from `flusher.py`, plus:

```python
    async def close(self) -> None:
        context, self._context = self._context, None
        if context is not None:
            await context.close_connections()
```

- [ ] **Step 3: Rewire the flusher**

- `__init__`: `self._connection = TortoiseConnection(config) if config is not None else None`; delete `self._context`.
- `_connection_ok`: `if self._connection is None: return True`; then `await self._connection.ensure()`.
- `_in_context`: `if self._connection is None: return await work()`; `context = await self._connection.open()`.
- `_save_batch_at_once` / `_save_in_context`: `if self._connection is not None: await self._connection.drop()`.
- `_close`: close through `self._connection.close()` when it exists.
- Delete `_open`, `_ensure_connection`, `_drop_context`, and now-unused imports (`asdict`, `TortoiseContext`, `context_config`, `check_connection`).

- [ ] **Step 4: Rewire `ScrapeDatabase`**

- `self._connection = TortoiseConnection(config)`; delete `_context`.
- `_serve_loop`: `await self._connection.close()`.
- `_in_context`: `context = await self._connection.ensure()`; close the coroutine and re-raise on `BaseException` exactly as today.
- Delete `_open`, `_ensure_connection`, `_drop_context`; drop the now-unused `TortoiseContext` import and the `flusher` imports of `check_connection`/`context_config`.

- [ ] **Step 5: Remove the repository's duplicate tuple**

In `app/repositories/google_map_profile_repository.py`, import `CONNECTION_LOST` from `app.services.postgres`, delete the local definition, and remove the `asyncpg` imports if nothing else uses them.

- [ ] **Step 6: Run the affected tests**

Run: `uv run pytest tests/services/buffer/test_flusher.py tests/services/buffer/test_flusher_crawl.py tests/services/google_map_scraper -q`
Expected: PASS, including the reconnect-retry tests.

- [ ] **Step 7: Commit**

```bash
git add app/services/postgres.py app/services/buffer/flusher.py app/services/google_map_scraper/scrape/scrape_database.py app/repositories/google_map_profile_repository.py tests/services/buffer/test_flusher.py
git commit -m "refactor: share one Postgres connection keeper"
```

---

### Task 5: Canonical repository counter

**Files:**
- Modify: `app/repositories/base.py`
- Modify: `app/repositories/lead_repository.py:31-42`
- Modify: `app/repositories/google_map_profile_repository.py:138-153`
- Modify: `app/repositories/scrape_repository.py:19-21`
- Test: `tests/repositories/` (existing counter tests)

**Interfaces:**
- Produces: `BaseRepository.__init__(self)` initialising `self._counters: dict = {}` and `async BaseRepository._next_global_counter(self) -> int`.
- Removes: both subclass copies of `_next_global_counter`.

- [ ] **Step 1: Run the existing counter tests as the guard**

Run: `uv run pytest tests/repositories -q`
Expected: PASS on the current code.

- [ ] **Step 2: Move the method into `BaseRepository`**

Add the `__init__` and move the identical body from `lead_repository.py` into `base.py`.

- [ ] **Step 3: Update the three subclasses**

Each `__init__` starts with `super().__init__()`; delete the two `_next_global_counter` copies.

- [ ] **Step 4: Run the repository tests**

Run: `uv run pytest tests/repositories -q`
Expected: PASS with unchanged assertions.

- [ ] **Step 5: Commit**

```bash
git add app/repositories/base.py app/repositories/lead_repository.py app/repositories/google_map_profile_repository.py app/repositories/scrape_repository.py
git commit -m "refactor: one global-counter implementation in BaseRepository"
```

---

### Task 6: Shared Redis list reads

**Files:**
- Create: `app/services/buffer/redis_lists.py`
- Modify: `app/services/buffer/crawl_queue.py:78-86`
- Modify: `app/services/buffer/website_queue.py:82-97`
- Test: `tests/services/buffer/test_crawl_queue.py`, `tests/services/buffer/test_website_queue.py`

**Interfaces:**
- Produces: `read_records(redis: Redis, key: str) -> list[dict[str, Any]]` and `sum_lengths(redis: Redis, *keys: str) -> int`.
- Removes: the duplicated list/pipeline bodies in both queue classes.

- [ ] **Step 1: Run the queue tests as the guard**

Run: `uv run pytest tests/services/buffer/test_crawl_queue.py tests/services/buffer/test_website_queue.py -q`
Expected: PASS on the current code.

- [ ] **Step 2: Create `app/services/buffer/redis_lists.py`**

```python
"""Small Redis list and hash reads shared by the buffer queues."""

import json
from typing import Any

from redis import Redis


def read_records(redis: Redis, key: str) -> list[dict[str, Any]]:
    return [json.loads(raw) for raw in redis.lrange(key, 0, -1)]


def sum_lengths(redis: Redis, *keys: str) -> int:
    pipe = redis.pipeline()
    for key in keys:
        pipe.llen(key)
    return sum(pipe.execute())
```

- [ ] **Step 3: Use them in both queues**

- `CrawlQueue.queued_jobs`: `return cast(list[CrawlJob], read_records(self._redis, crawl_queue_key()))`.
- `CrawlQueue.pending`: `return sum_lengths(self._redis, crawl_queue_key(), crawl_processing_key())`.
- `WebsiteQueue.processing_jobs` / `queued_jobs`: same with their keys.
- `WebsiteQueue.pending`: `sum_lengths(self._redis, queue_key(scrape_id), processing_key(scrape_id))`.

- [ ] **Step 4: Run the queue tests**

Run: `uv run pytest tests/services/buffer -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/services/buffer/redis_lists.py app/services/buffer/crawl_queue.py app/services/buffer/website_queue.py
git commit -m "refactor: share Redis list reads between buffer queues"
```

---

### Task 7: Verify the whole change

- [ ] **Step 1: Full suite**

Run: `uv run pytest -q`
Expected: PASS. The count will be below 918 because the `log` tests were deleted.

- [ ] **Step 2: Lint**

Run: `uv run ruff check .`
Expected: exactly the same 4 pre-existing errors as before the change.

- [ ] **Step 3: Refresh the knowledge graph**

Run: `graphify update .`
Expected: graph rebuilt without errors.

- [ ] **Step 4: Confirm no duplicate survived**

Run:
```bash
grep -rn "def _now\|def _header\|def _drop_context\|def _next_global_counter\|def _host" app/ --include=*.py
```
Expected: no output.

- [ ] **Step 5: Commit any graph/manifest updates**

```bash
git add graphify-out
git commit -m "chore: refresh knowledge graph after helper dedup"
```
