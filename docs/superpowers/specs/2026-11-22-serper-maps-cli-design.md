# Serper Maps CLI: a paid, key-pooled Google Maps scrape

Date: 2026-11-22
Status: implemented

## Intent

The browser scraper in `app/services/google_map_scraper/` is slow, fragile and
rate-limited by Google. Serper's `/maps` endpoint returns the same kind of place
data as clean JSON, 20 places per call, for 3 credits. Serper also gives several
keys, each with its own balance and its own 5-requests-per-second limit. The
partner wants the same job the browser CLI does — keyword(s) across a city,
split into a grid, several pages deep — run through Serper, with the work spread
across all the keys, stored in PostgreSQL, and resumable when the credits run
out.

```bash
serper-map-scrape \
  --keyword plumbers --depth 2 --grid 3 \
  --city Austin --state TX --country us
```

## What exists already

- `app/services/serper/maps/serper_map_api.py` is a one-shot script: a module-level
  `requests.Session`, a hardcoded payload and a `print`. It is not a client.
- `app/services/serper/maps/serper_map_type.py` declares dataclasses, but
  `from_api` passes raw dicts straight into `SearchParameters`, `OpeningHours` and
  `Place`, so the declared types are not actually built. The field names are
  Serper's (`placeId`, `phoneNumber`, `ratingCount`, `openingHours`).
- `geo.py` already does what this needs: `get_geocode(place, with_bbox=True)`
  returns the city centre and Nominatim's bounding box, and `build_grid_cells`
  returns one `{lat, lon, radius, zoom}` per tile for an N x N split.
- `GoogleMapProfileRepository.save_rows` already saves `LeadRow`s in bulk with
  duplicate matching on `data_id` / `place_id` / `title`, and
  `key_rows` + `seen_keys_of` give the CLI the known-key set to skip.
- `LeadCrawlability` marks `is_crawlable` on rows by fetching each website origin.

## The run, in order

1. **Parse.** `--keyword` (repeatable), `--city`, `--state`, `--country`, `--depth`,
   `--grid`, `--parallel`, `--has-phone`, `--has-website`, `--dry-run`, plus the
   resume flags described below. Validation is pydantic, the way the existing CLI
   does it, and an invalid flag exits with the reason.
2. **Geocode once** through `get_geocode(place, with_bbox=True, country=country)`.
   The centre point is used when `grid == 1`; the bounding box is split with
   `build_grid_cells(bbox, grid)` otherwise. No geocode, no run — that is a hard
   error, not a partial scrape.
3. **Build the request list in memory.** For every keyword: every grid cell (or
   the one city centre) × page `1..depth`. Each entry is
   `(keyword, lat, lng, zoom, grid_index, page)`, and the list keeps that order.
   A duplicate cell centre is collapsed.
4. **Open the job.** A `serper_map_jobs` row is created with the full config and
   the request count; one `serper_map_requests` row per entry, all `pending`.
5. **Load known keys** through `GoogleMapProfileRepository.key_rows()` so rows
   already stored are skipped.
6. **Drain the queue across the keys.** One worker per API key, each firing
   `--parallel` requests at a time, then sleeping 1.5 s before its next batch.
7. **Save each page's rows as it arrives**, after mapping them to `LeadRow`,
   applying the filters, and (when `--has-website`) the crawlable check.
8. **Close the job**: `finished` when every request succeeded, `exhausted` when
   every key ran dry with work left, `interrupted` on Ctrl-C, `failed` on an
   unrecoverable error.

## Key pool and pacing

- Keys come from `SERPER_API_KEYS` (comma-separated) in `.env` only. No CLI flag.
  Duplicates are dropped, an empty value is a startup error naming the variable.
- Each key is a `KeySlot` with a lock, a `state` of `ready` / `dry` / `dead`, and
  its own semaphore of `--parallel` slots (default 5, max 20). A key's worker
  claims the next pending request, fires it, waits 1.5 s after its batch of
  `--parallel` finishes, and repeats. Batches are per key, so four keys with
  `--parallel 5` mean up to 20 requests in flight, five per key, and the rest
  after each key's own rest.
- A request's outcome decides the slot's fate:
  - HTTP 200 → slot stays `ready`.
  - HTTP 402, or 403 with a body naming credits/quota/plan, or a `credits`
    balance below the cost → slot becomes `dry`; the request goes back to
    `pending` for another key and is not counted as a failure.
  - HTTP 401/403 "Unauthorized" → slot becomes `dead`, because retrying a bad key
    on another key is fine but retrying the bad key is not.
  - HTTP 429 → the slot waits for the `Retry-After` (or 2 s) and retries the same
    key; three 429s in a row mark the slot `dead` for this run.
  - HTTP 5xx or a network error → retried on this key twice, then the request
    returns to `pending`; three such failures on one request mark it `failed`.
  - Any other 4xx → the request is `failed` with the body's message.
- When every slot is `dry` or `dead` and work remains, the run stops cleanly:
  unfinished requests stay `pending`, the job becomes `exhausted`, the exit code
  is 0 with a message that says the credits ran out. When every slot is `dead`
  for auth reasons, the job becomes `failed` and the exit code is 1 — a key problem
  is not "come back later".
- `--parallel` is per key, because Serper's own `/account` reports
  `rateLimit: 5` per key. It is documented that way in `--help`.
- Credit accounting: the response's `credits` field is the cost of the call (3),
  not a balance. The key's balance comes from `GET /account` (`balance`), read
  once at startup and after any key returns "out of credits", so a key with less
  than 3 left is retired before it burns a request. The per-request `credits`
  cost is still stored, so the job's total spend is visible.

## Resume

- On startup, before geocoding, the CLI looks for the newest job matching the
  config it just parsed (keyword set, city, state, country, depth, grid) whose
  status is `pending`, `running`, `exhausted` or `interrupted`. That is "the same
  config that was passed", per the partner.
- If one exists it prints the job's id, status, when it last ran, how many
  requests are done / pending / failed, and asks one question:
  `Continue this job? [Y/n]`. `Y` reuses the job and its existing request rows:
  `done` requests are skipped, `failed` and `pending` ones are retried. `n`
  creates a fresh job with the same config and leaves the old one alone.
- If no matching job exists, a new one is created without asking. A job whose
  every request is `done` is `finished` and is not offered.
- A job left `running` (the process was killed) is offered like any other
  unfinished job and simply continues; there is no separate "was it really
  running" check.
- Running is idempotent at the database level: profiles and leads are matched by
  the repository, so a page re-fetched into a resumed job updates stored rows
  instead of duplicating them.

## Storage

Two new tables, plus the existing lead tables. Both follow the `ScrapeModel`
style (UUID pk, `created_at`, `updated_at`).

`serper_map_jobs`

| column | type | notes |
| --- | --- | --- |
| `id` | UUID | pk |
| `name` | varchar(200) | e.g. `plumbers @ Austin, TX, US` |
| `status` | varchar(16), indexed | `running` / `exhausted` / `interrupted` / `finished` / `failed` |
| `error` | text | last error, empty when none |
| `config` | JSON | keywords, city, state, country, depth, grid, parallel, filters, lat/lon/zoom, bbox |
| `key_count` | int | keys used; never the keys themselves |
| `total_requests` | int | size of the queue |
| `done_requests` | int | pages with a 200 and a save attempt |
| `failed_requests` | int | pages that ended in a permanent failure |
| `places_found` | int | raw places returned |
| `places_saved` | int | rows written after dedupe and filters |
| `credits_spent` | int | sum of the `credits` in responses |
| `created_at`, `updated_at` | datetime | |

`serper_map_requests`

| column | type | notes |
| --- | --- | --- |
| `id` | UUID | pk |
| `job` | FK → `serper_map_jobs` | cascade |
| `keyword` | varchar(500) | |
| `page` | int | 1-based |
| `grid_x`, `grid_y` | int | 0 when there is no grid |
| `lat`, `lng` | float | the tile centre |
| `zoom` | int | from `build_grid_cells` |
| `status` | varchar(16), indexed | `pending` / `done` / `failed` |
| `attempts` | int | calls made, across keys |
| `key_hint` | int, nullable | index of the key that succeeded last (never the key) |
| `http_status` | int, nullable | |
| `error` | text | empty when none |
| `places` | int | places returned |
| `credits` | int | credits the response reported |
| `created_at`, `updated_at` | datetime | |

Unique together on `(job, keyword, page, grid_x, grid_y)`, so building the queue
twice, or resuming, cannot duplicate a request row.

Migration `0006_serper_map_tables.py`, depending on `0005`, created with
`ops.CreateModel` like the existing ones, excluded from ruff like its siblings.

## Row mapping

`Place` (Serper) → `LeadRow` (existing), keeping the existing dedupe keys alive:

| LeadRow | from Serper |
| --- | --- |
| `title` | `title` |
| `address` | `address` |
| `phone` | `phoneNumber`, formatted for the country with `format_phone_numbers` so `--has-phone` behaves like the browser CLI |
| `website` | `website` |
| `categories` | `types` joined with `" | "`, which `address_fields`/`_split` expect |
| `latitude`, `longitude` | `latitude`, `longitude` |
| `cid` | `cid` |
| `data_id` | `fid` (it is `0x…:0x…`, the shape the parser already turns into a CID) |
| `place_id` | `placeId` |
| `link` | the Google Maps place URL for `title` + `placeId` |
| `open_hours` | `openingHours` as JSON |
| `review_count` | `ratingCount` |
| `review_rating` | `rating` |
| `thumbnail` | `thumbnailUrl` |
| `descriptions` | `type` (the primary category) |
| `input_id` | the keyword that found it |
| `complete_address` | `""` — Serper gives only the one-line address |
| everything else | `""` / `None`, so the repository's `fill_empty` never blanks stored data |

`is_crawlable` is set only when `--has-website` is on, using `LeadCrawlability`,
matching the browser scraper (`check_crawlable=settings.has_website`).

## Filters

- `--has-phone` / `--has-website` drop rows before saving, reusing `keep_lead`
  with a settings object, exactly like the browser CLI.
- A filter never stops the page loop: the page still counts as `done`.
- Filters apply before the crawlable check, so only kept rows are fetched.

## Duplicate check

- Startup loads the known key set once (`key_rows` → `seen_keys_of`), as the
  browser CLI does.
- Every page's rows are deduped against that set and against each other with
  `dedupe_rows`, and the set is updated as rows are accepted, so two tiles or two
  pages returning the same business save it once.
- The repository still matches by `data_id` / `place_id` / `title` on write, so a
  resumed job that lost the in-memory set cannot create a duplicate.

## Module layout

The service is the product; the CLI is one client of it. Nothing under
`app/services/serper/` may parse argv, print to stdout, prompt, or call
`sys.exit`. A REST route and a taskiq worker must be able to call the same
functions with no CLI present.

```
app/services/serper/maps/
  serper_map_type.py         # response types: Place, OpeningHours, SearchResult (typed)
  serper_map_client.py       # one HTTP call: (key, keyword, ll, page) -> SearchResult | error
  serper_map_config.py       # SerperMapConfig (pydantic), the validated run request
  serper_map_plan.py         # geocode + cells + pages -> SerperMapPlan (the request list)
  serper_map_state.py        # KeySlot, key pool, the per-key batch loop, the drain
  serper_map_rows.py         # Place -> LeadRow, filters
  serper_map_service.py      # the facade: plan / start / resume / cancel, progress events
  serper_map_errors.py       # SerperError, NoCreditsError, AuthError, RateLimitedError
app/repositories/
  serper_map_repository.py   # job + request rows: create, resume-lookup, claim, finish
app/models/
  serper_map_model.py        # SerperMapJobModel, SerperMapRequestModel
cli/
  serper_map_scrape_cli.py   # argparse + prompt + printing; calls the service only
```

`serper_map_api.py` is replaced by `serper_map_client.py`; keeping both would
leave two ways to call the same endpoint. The module-level script goes away.
`serper_map_type.py` gains the nested builders (`from_api` returns real
dataclasses with `OpeningHours` and typed `Place`s) instead of raw dicts.

Concurrency is plain `asyncio`: `httpx.AsyncClient` (already a dependency, used
by `website_resolver.py`) with one client per key, an `asyncio.Queue` of pending
requests, and one `asyncio.Task` per key.

## The service contract

`SerperMapService` is the only thing a caller needs. It is constructed with
already-open dependencies, so the caller owns the Tortoise lifecycle:

```python
class SerperMapService:
    def __init__(
        self,
        config: SerperMapConfig,
        *,
        api_keys: Sequence[str],
        profiles: GoogleMapProfileRepository | None = None,
        known_keys: frozenset[str] | None = None,
        on_event: Callable[[SerperMapEvent], None] | None = None,
    ) -> None: ...

    async def plan(self) -> SerperMapPlan: ...            # geocode + tiles + pages, no writes
    async def find_unfinished(self) -> SerperMapJobInfo | None: ...  # same-config unfinished job
    async def start(self) -> SerperMapJobInfo: ...        # create the job + request rows
    async def resume(self, job_id: UUID) -> SerperMapJobInfo: ...    # retry pending/failed
    async def run(self, job_id: UUID) -> SerperMapResult: ...        # drain until done/exhausted
    async def cancel(self) -> None: ...                   # stop the drain, keep progress
```

Rules that make it reusable:

- **Config in, result out.** `SerperMapConfig` is a pydantic model carrying
  keywords, city, state, country, depth, grid, parallel, filters. `SerperMapResult`
  is a frozen dataclass: `job_id`, `status`, `requests_done`, `requests_failed`,
  `requests_pending`, `places_found`, `places_saved`, `credits_spent`, `error`.
  No dicts of loosely-typed values cross the boundary.
- **Progress is an event, not a print.** `on_event(SerperMapEvent)` receives
  `started`, `request_done`, `request_failed`, `key_dry`, `page_saved`,
  `progress`, `finished`. A CLI formats them; a REST caller can push them to a
  queue; a worker can log them. The event type is a frozen dataclass with a
  `kind` field and only optional payload fields.
- **No process control.** The service raises typed errors from
  `serper_map_errors.py`; it never exits. `asyncio.CancelledError` from a caller's
  `task.cancel()` is caught, the job is marked `interrupted`, and the existing
  progress stays in the tables — that is the same path Ctrl-C takes.
- **Keys are injected, never read from the environment inside the service.**
  `SerperMapConfig` does not hold keys and no service module calls `os.getenv`.
  The CLI reads `SERPER_API_KEYS` and passes the list; a worker or API will read
  it from wherever it likes.
- **Database access is the repository's job.** `SerperMapRepository` owns
  `serper_map_jobs` and `serper_map_requests`; the lead writes go through
  `GoogleMapProfileRepository` exactly as the browser engine does. The service
  composes both, so no SQL lives in the drain loop.
- **The run is single-job.** A service instance runs one job; the caller starts a
  second instance for a second job. There is no shared mutable module state, so a
  worker can run several concurrently.

## CLI surface

```
--keyword / -k     repeatable; at least one
--city             required
--state            optional
--country          required, 2 letters
--depth            pages per tile, 1..10, default 1
--grid             N x N tiles, 1..10, default 1
--parallel         requests per API key at a time, 1..20, default 5
--has-phone        keep rows with a phone
--has-website      keep rows with a website, and check crawlability
--dry-run          print the plan (tiles, pages, requests, keys, spend) and exit
--no-resume        never ask; always start a new job
```

`--depth` here means Serper pages, not browser scroll batches: depth 2 is pages
1 and 2 per tile. The help text says so. Cost is printed in the plan: 3 credits
× tiles × pages.

## Goals

- One command runs keyword(s) across a city grid through Serper, storing the
  results in the existing lead tables.
- Every API key is used in parallel, each at its own concurrency and its own
  1.5 s rest, and the run survives a key running dry.
- Unfinished work is visible in PostgreSQL and resumable with the same config.
- Duplicates are skipped, before and during the write.
- The phone, website and crawlable behaviour matches the browser CLI.
- The scrape is a reusable service with a typed config in and a typed result
  out; the CLI is only its first caller, so a REST route or a taskiq worker can
  use it unchanged.

## Non-goals

- Replacing the browser scraper, or sharing its Redis tile queue.
- A REST endpoint, a web UI, or a scheduler. Those come later and call the
  service; this plan builds only the service and its CLI.
- Per-keyword depth or grid, deep scan, sub-tiles, proxies.
- Collecting place details beyond what `/maps` returns (no reviews text, no
  images, no emails, no socials).
- Storing or logging API keys.

## Anti-goals (the work failed if any of these happen)

- A key is printed, logged, or stored in a table.
- One key's exhaustion stops the other keys.
- A key that ran dry is retried for the rest of the run.
- A resumed job re-fetches pages that were already `done`.
- A resumed job duplicates a lead already in PostgreSQL.
- `--parallel` silently caps the run at that number overall instead of per key.
- Ctrl-C loses the pages already fetched.
- An exhausted run is marked `failed`.
- The service imports argparse, calls `print`, prompts, or exits the process.
- A second caller (route, worker) needs the CLI to drive the service.

## What is left to the builder

- Exact httpx client settings (timeouts, connection limits per key).
- The precise `Retry-After` handling and backoff for 429.
- Whether the per-key rest is a single `asyncio.sleep(1.5)` or a small jittered
  sleep; 1.5 s is the floor either way.
- Log line wording and the progress line format.
- Test split and fixtures.
