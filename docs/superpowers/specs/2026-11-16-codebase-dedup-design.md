# Unify duplicated helpers across the codebase

Date: 2026-11-16
Status: design, awaiting approval

## Intent

The same small job is implemented two to four times under different names, and the
copies have started to drift: one host helper keeps the port, one drops it; one
header helper returns `None`, another returns `""`; two Postgres connection keepers
are byte-identical except for where they read their config. Every copy is a place
where a later fix lands in one call site and silently skips the others.

The goal is one canonical implementation per concept, with every caller on it and
the tests updated to match. The partner chose **canonical behaviour plus caller and
test updates**, not per-call-site behaviour preservation.

## Goals

- Collapse six confirmed duplicate families into one canonical helper each.
- Delete code that has no callers: `app.utils.utils.log` and
  `BaseRepository.now` (plus their tests).
- Keep the public module layout stable except for the two new modules listed below.
- Leave the suite green and the existing (pre-existing) ruff baseline unchanged.

## Non-goals

- Unifying `__init__`, `__enter__`/`__exit__`, or `to_dict` boilerplate. Those
  copies are shorter than the abstraction that would replace them.
- Touching `app/helpers/http_fetcher/urls.py::hostname_of`; it deliberately keeps
  `www.` because DNS checks need the full name. It is a different contract, not a
  duplicate.
- Changing API payload formatting such as
  `scrape_repository._to_record`'s `isoformat(timespec="seconds")`.
- Refactoring test fixtures or test helpers.
- Renaming packages beyond the two new modules below.

## Anti-goals (the work failed if any of these happen)

- Losing information stored today, for example truncating Redis timestamps to
  whole seconds.
- A shared base class that couples classes which do not share behaviour, just to
  remove a few lines.
- Any change to Redis keys, Postgres tables, or Redis/Postgres row shapes beyond
  the timestamp precision noted below.
- A repo-wide import rewrite for its own sake.

## Constraints

- Python 3.12, `ruff`, `pyright` (standard), `pytest` with `asyncio_mode = "auto"`.
- Buffer tests use `fakeredis`; DB tests are behind the `postgres` marker.
- No new dependencies.
- After the change, run `graphify update .` so the knowledge graph matches the code
  (required by `AGENTS.md`).

## Canonical decisions

### F1. Time helpers — `app/utils/utils.py`

Canonical: `utc_now() -> str` returns `datetime.now(UTC).isoformat()` (microseconds).
Microseconds win over seconds because they carry more information, `fromisoformat`
parses both, and truncating would be irreversible.

Add `isoformat_or_none(value: datetime | None) -> str | None` in the same module.

Callers to update:
- `app/services/buffer/crawl_buffer.py`: delete `_now`, import and use `utc_now`.
- `app/http/resources/lead_resource.py`: `Resource.iso` delegates to
  `isoformat_or_none` (or is replaced by it at call sites).
- Delete `app/utils/utils.py::log` (no callers outside `tests/utils/test_utils.py`).
- Delete `BaseRepository.now` (no callers).

Not touched: `scrape_repository._to_record` (a display format, not a duplicate
function).

### F2. Host extraction — `app/utils/url_utils.py::extract_site_host`

Canonical: `extract_site_host(url) -> str` — **strips surrounding whitespace**,
lower-cases the host, parses it as a URL (adding `http://` when there is no
scheme), strips a leading `www.`, returns `""` when there is no host.

The strip moves into the canonical helper, not left to `website_domain`. Today
`website_domain` strips and `extract_site_host` does not, so the naive
`extract_site_host(website) or None` would write `"  "` into the unique
`leads.website_domain` column and return `"  https"` for a padded URL. Putting the
strip in the helper also makes `same_scope` tolerant of padded input, which is
strictly better.

No data migration is needed: the old `website_domain` already stripped before
writing the column, so stored values are already canonical. Only the lookup path
(`find_by_host` re-reads `LeadModel.website`) needed the tolerance, and it keeps it.

Callers to update:
- `app/repositories/lead_repository.py::website_domain` becomes
  `extract_site_host(website) or None`, preserving its `str | None` contract.
- `app/services/crawler/extractors/seo.py`: delete `_host`, use `extract_site_host`.
  This is a deliberate behaviour change: `_host` used `netloc`, so it kept the
  port; same-site comparison now ignores the port.
- `app/services/buffer/crawl_sink.py`: replace the inline
  `snapshot.website["host"].removeprefix("www.")` with `extract_site_host(...)`.

### F3. Response header lookup — `app/helpers/http_fetcher/responses.py`

Canonical: `header_value(headers: Mapping[str, str], name: str) -> str` —
case-insensitive lookup, `""` when the header is absent. It takes the header
mapping rather than `FetchResult` because `models.py` already imports from
`responses.py`; importing `FetchResult` back would create a cycle.

Callers to update:
- `app/services/crawler/pipeline/crawlability.py`: delete `_header`; `is_crawlable`
  already treats a missing header as not crawlable, so `""` preserves behaviour.
- `app/services/crawler/site/crawl.py`: delete `_header`; callers already do
  `[:255] or None` or `.split(";")`, both of which work on `""`.

`header_value` goes in `responses.py` so it sits beside `is_success_status` and
`normalize_headers`, which operate on the same `FetchResult` data.

### F4. Postgres connection plumbing — new `app/services/postgres.py`

Move from `app/services/buffer/flusher.py` into the new module:
`CONNECTION`, `CONNECTION_LOST`, `context_config`, `check_connection`.

`CONNECTION_LOST` is included because `app/repositories/google_map_profile_repository.py`
defines an identical tuple and uses it to re-raise connection failures instead of
swallowing them. Both modules import the canonical one. The repository already
imports from `app.services` (types), and the new module imports nothing from the
app package, so this creates no cycle.

Add `class TortoiseConnection` owning `(config, context)` with:
- `async open() -> TortoiseContext` — opens once, same init/rollback/logging as today.
- `async ensure() -> TortoiseContext` — `open`, ping, on failure `drop` then reopen.
- `async drop() -> None` — the byte-identical `_drop_context` body.
- `async close() -> None` — close the live context.
- `context` property for read access.

Callers to update:
- `app/services/buffer/flusher.py`: replace `_open`, `_ensure_connection`,
  `_drop_context` with the keeper; keep `_in_context` (its callable-based retry
  logic is flusher-specific) and `_close` (loop/thread shutdown).
- `app/services/google_map_scraper/scrape/scrape_database.py`: replace `_open`,
  `_ensure_connection`, `_drop_context`; keep `_in_context` (its coroutine-close
  and `_call` blocking bridge are scraper-specific) and `_serve_loop`.
- `app/repositories/google_map_profile_repository.py`: import `CONNECTION_LOST`
  from the new module and delete the local tuple.
- This also removes the scraper's import of the buffer's flusher module.
- Update `tests/services/buffer/test_flusher.py` which reaches into
  `flusher._context`; use the keeper's `context` instead.

### F5. Global counter — `app/repositories/base.py`

Move `_next_global_counter` into `BaseRepository`; add
`BaseRepository.__init__` that sets `self._counters: dict = {}`.
`LeadRepository`, `GoogleMapProfileRepository`, and `ScrapeRepository` call
`super().__init__()`; the two copies are deleted.

### F6. Redis list reads — new `app/services/buffer/redis_lists.py`

Add module functions, no base class:
- `read_records(redis, key) -> list[dict]` — `lrange(key, 0, -1)` plus `json.loads`
  per element, typed by the caller.
- `sum_lengths(redis, *keys) -> int` — pipelined `llen` plus `sum`.

Callers: `CrawlQueue.queued_jobs`, `CrawlQueue.pending`,
`WebsiteQueue.processing_jobs`, `WebsiteQueue.queued_jobs`,
`WebsiteQueue.pending`. The classes and their script registration stay as they are.

## Behaviour changes, in one place

| Where | Before | After | Why it is acceptable |
|---|---|---|---|
| `utc_now`/`crawl_buffer` timestamps | microseconds in Redis, seconds only in `utc_now`'s unused callers | microseconds everywhere | `fromisoformat` reads both; no precision lost |
| `Resource.iso` | inline conditional | shared helper | identical output |
| `seo` same-site check | host with port | host without port | port is not part of a site's identity |
| `extract_site_host` | kept padding (`"  x  "` → `""`) | strips padding first (`"  x  "` → `x`) | parsing a padded URL is what every caller meant |
| header missing | `None` in crawlability, `""` in crawl | `""` everywhere | `is_crawlable` already returns `False` for both |
| `website_domain("")` | `None` | `None` | empty canonical host mapped back to `None` |

## Test plan

Update:
- `tests/utils/test_utils.py`: expect microseconds, drop the `log` tests.
- `tests/utils/test_url_utils.py`: add padded-input cases for `extract_site_host`
  and `same_scope`.
- `tests/repositories/test_lead_repository_website.py`: confirm `website_domain`
  keeps returning `None` for empty input, and that padded/`www.` input still maps
  to the bare host.
- Crawler extractor tests that assert internal/external host handling, if any use a
  URL with a port.
- `tests/services/buffer/test_flusher.py`: read `flusher`'s keeper context instead
  of `flusher._context`.

Verify:
- `uv run pytest -q`
- `uv run ruff check .` — must stay at the same 4 pre-existing errors, no new ones.
- `graphify update .`

## What is left to the builder

- Exact naming/shape of `TortoiseConnection`'s public surface (`context` property vs
  method, constructor signature) as long as the retry semantics above are kept.
- Whether `Resource.iso` stays as a thin wrapper or is replaced by direct calls.
- Commit splitting and the order in which families are migrated.
- Test updates that are mechanically implied by the table above.
