# Serper Maps Service and CLI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a reusable Serper Maps scraping service (typed config in, typed result out) that spreads keyword × grid-tile × page work across a pool of API keys in parallel, stores places in the existing lead tables with dedupe and filters, records each job and request in PostgreSQL, and is resumable — plus a thin CLI that is only its first caller.

**Architecture:** A client wraps one Serper `/maps` call. A planner turns a `SerperMapConfig` into a list of `SerperMapPage` requests via the existing `get_geocode`/`build_grid_cells`. A scheduler drains that list with one asyncio task per API key, each firing `parallel` requests then resting 1.5 s, retiring keys that run dry. A service facade owns create/resume/run/cancel and emits events instead of printing. A repository stores jobs and requests; places go through the existing `GoogleMapProfileRepository`.

**Tech Stack:** Python 3.12, asyncio, httpx (already a dependency), pydantic v2, Tortoise ORM 1.x + asyncpg, pytest/pytest-asyncio.

**Spec:** `docs/superpowers/specs/2026-11-22-serper-maps-cli-design.md`

## Global Constraints

- Service modules (`app/services/serper/**`) must not import `argparse`, call `print`, prompt, read `os.getenv`, or call `sys.exit`. Typed errors only.
- API keys come from `SERPER_API_KEYS` (comma-separated) in `.env`, read by the CLI only. Never log, print, or store a key. `key_hint` in the database is the key's 0-based index.
- Serper `/maps` costs 3 credits per call and returns at most 20 places per page, regardless of `num`.
- `--parallel` is per API key, default 5, range 1..20. Serper reports `rateLimit: 5` per key, and `x-ratelimit-limit: 25` per window.
- Each key rests 1.5 s between batches. The next batch starts only after the key's `parallel` requests finish.
- Job statuses: `running`, `exhausted`, `interrupted`, `finished`, `failed`. Request statuses: `pending`, `done`, `failed`.
- Every exhausted run exits 0 and stays resumable. Auth failures with work left are `failed`, exit 1.
- Depth is pages per tile, 1..10, default 1. Grid is N×N, 1..10, default 1.
- New tables: `serper_map_jobs`, `serper_map_requests`. Migration `0006_serper_map_tables`.
- All new Python files are inside the validated trees (`app/`, `cli/`) so `ruff` and `pyright` cover them. `app/migrations/**` stays excluded from both.
- Tests use the existing `db` fixture (`tests/models/conftest.py`) for SQLite in-memory Tortoise.

## Review Focus

- **A key that runs dry mid-batch.** Its in-flight sibling requests must finish against it, its queued request must return to `pending` for another key, and the key must never be selected again in that run. Expected: no request is lost, no `failed` status, run continues on the other keys.
- **All keys dry with work left.** Expected: job `exhausted`, exit 0, remaining requests still `pending`, and a rerun offers the same job.
- **Resume after an interrupted run.** Expected: `done` pages are not re-fetched, `pending`/`failed` pages are, and no lead is duplicated in `leads`/`google_maps_profiles`.
- **Ctrl-C in the middle of a batch.** Expected: pages already fetched are saved, the job is `interrupted` not `failed`, and progress counters match the rows.
- **A page that returns 0 places.** Expected: recorded `done` with 0 places (not retried, not failed), because pages past the result cap legitimately return nothing.

---

### Task 1: Job and request tables

**Files:**
- Create: `app/models/serper_map_model.py`
- Modify: `app/models/__init__.py`
- Create: `app/migrations/0006_serper_map_tables.py`
- Test: `tests/models/test_serper_map_model.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `SerperMapJobModel`, `SerperMapRequestModel`, `SerperMapJobStatus` (`running`/`exhausted`/`interrupted`/`finished`/`failed`), `SerperMapRequestStatus` (`pending`/`done`/`failed`).

- [ ] **Step 1: Write the failing test**

```python
async def test_a_job_stores_its_config_and_counts(db):
    from app.models import SerperMapJobModel, SerperMapRequestModel, SerperMapJobStatus

    job = await SerperMapJobModel.create(
        name="plumbers @ Austin",
        status=SerperMapJobStatus.RUNNING,
        config={"keywords": ["plumbers"], "city": "Austin", "depth": 2, "grid": 3},
        config_key="k1",
        key_count=4,
        total_requests=18,
    )
    await SerperMapRequestModel.create(
        job_id=job.id, keyword="plumbers", page=1, grid_x=0, grid_y=0,
        lat=30.1, lng=-97.7, zoom=13,
    )
    assert job.status == "running"
    assert job.done_requests == 0 and job.credits_spent == 0
    requests = await SerperMapRequestModel.filter(job_id=job.id)
    assert [r.status for r in requests] == ["pending"]
    assert requests[0].attempts == 0 and requests[0].key_hint is None


async def test_a_request_is_unique_per_job_keyword_page_and_grid(db):
    from tortoise.exceptions import IntegrityError
    from app.models import SerperMapJobModel, SerperMapRequestModel, SerperMapJobStatus

    job = await SerperMapJobModel.create(
        name="j", status=SerperMapJobStatus.RUNNING, config={}, config_key="k", total_requests=1
    )
    fields = dict(job_id=job.id, keyword="plumbers", page=2, grid_x=1, grid_y=0,
                  lat=30.1, lng=-97.7, zoom=13)
    await SerperMapRequestModel.create(**fields)
    with pytest.raises(IntegrityError):
        await SerperMapRequestModel.create(**fields)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/models/test_serper_map_model.py -v`
Expected: FAIL — `ImportError: cannot import name 'SerperMapJobModel'`

- [ ] **Step 3: Implement the models**

`app/models/serper_map_model.py`:

```python
class SerperMapJobStatus:
    RUNNING = "running"
    EXHAUSTED = "exhausted"
    INTERRUPTED = "interrupted"
    FINISHED = "finished"
    FAILED = "failed"


class SerperMapRequestStatus:
    PENDING = "pending"
    DONE = "done"
    FAILED = "failed"


class SerperMapJobModel(Model):
    id = fields.UUIDField(primary_key=True, default=uuid4)
    name = fields.CharField(max_length=200)
    status = fields.CharField(max_length=16, db_index=True)
    error = fields.TextField(default="")
    config = fields.JSONField()
    config_key = fields.CharField(max_length=64, db_index=True)
    key_count = fields.IntField(default=0)
    total_requests = fields.IntField(default=0)
    done_requests = fields.IntField(default=0)
    failed_requests = fields.IntField(default=0)
    places_found = fields.IntField(default=0)
    places_saved = fields.IntField(default=0)
    credits_spent = fields.IntField(default=0)
    created_at = fields.DatetimeField(auto_now_add=True)
    updated_at = fields.DatetimeField(auto_now=True)
    requests: fields.ReverseRelation["SerperMapRequestModel"]

    class Meta(Model.Meta):
        table = "serper_map_jobs"


class SerperMapRequestModel(Model):
    id = fields.UUIDField(primary_key=True, default=uuid4)
    job = fields.ForeignKeyField(
        "models.SerperMapJobModel", related_name="requests", on_delete=fields.CASCADE
    )
    keyword = fields.CharField(max_length=500)
    page = fields.IntField()
    grid_x = fields.IntField(default=0)
    grid_y = fields.IntField(default=0)
    lat = fields.FloatField()
    lng = fields.FloatField()
    zoom = fields.IntField()
    status = fields.CharField(max_length=16, db_index=True, default="pending")
    attempts = fields.IntField(default=0)
    key_hint = fields.IntField(null=True)
    http_status = fields.IntField(null=True)
    error = fields.TextField(default="")
    places = fields.IntField(default=0)
    credits = fields.IntField(default=0)
    created_at = fields.DatetimeField(auto_now_add=True)
    updated_at = fields.DatetimeField(auto_now=True)

    class Meta(Model.Meta):
        table = "serper_map_requests"
        unique_together = (("job", "keyword", "page", "grid_x", "grid_y"),)
```

Export both models from `app/models/__init__.py` and add them to `__all__`.

`app/migrations/0006_serper_map_tables.py`: follow `0004`'s style — `dependencies = [("models", "0005_remove_unique_idx")]`, `initial = False`, two `ops.CreateModel` operations with the fields above, JSON columns encoded with `functools.partial(dumps, separators=(",", ":"))` and `loads` like `0001_initial.py`, and `unique_together` in `options`.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/models/test_serper_map_model.py -v`
Expected: PASS (2 passed)

- [ ] **Step 5: Verify the migration applies**

Run: `uv run python artisan db:migrate` (needs `DB_URL`; if no database is reachable, note it and rely on the models test, which builds the schema from the models)
Expected: migration `0006` applied without error.

- [ ] **Step 6: Commit**

```bash
git add app/models/serper_map_model.py app/models/__init__.py app/migrations/0006_serper_map_tables.py tests/models/test_serper_map_model.py
git commit -m "feat(serper): add job and request tables"
```

---

### Task 2: Job repository

**Files:**
- Create: `app/repositories/serper_map_repository.py`
- Modify: `app/repositories/__init__.py`
- Test: `tests/repositories/test_serper_map_repository.py`

**Interfaces:**
- Consumes: Task 1 models.
- Produces:
  - `SerperMapPage` (frozen dataclass): `keyword: str`, `page: int`, `grid_x: int`, `grid_y: int`, `lat: float`, `lng: float`, `zoom: int`; property `ll -> str` returning `f"@{lat},{lng},{zoom}z"`.
  - `SerperMapJobInfo` (frozen dataclass): `id: UUID`, `name: str`, `status: str`, `error: str`, `config: dict`, `total_requests: int`, `done_requests: int`, `failed_requests: int`, `pending_requests: int`, `places_found: int`, `places_saved: int`, `credits_spent: int`, `created_at: datetime`, `updated_at: datetime`.
  - `SerperMapRepository` with:
    - `async def create_job(self, name: str, config: dict, config_key: str, pages: list[SerperMapPage], key_count: int) -> SerperMapJobInfo`
    - `async def find_unfinished(self, config_key: str) -> SerperMapJobInfo | None`
    - `async def get(self, job_id: UUID) -> SerperMapJobInfo | None`
    - `async def pending_pages(self, job_id: UUID) -> list[SerperMapPage]`
    - `async def mark_done(self, job_id: UUID, page: SerperMapPage, *, http_status: int, places: int, credits: int, key_hint: int, saved: int) -> None`
    - `async def mark_failed(self, job_id: UUID, page: SerperMapPage, *, http_status: int | None, error: str) -> None`
    - `async def mark_attempt(self, job_id: UUID, page: SerperMapPage, key_hint: int) -> None`
    - `async def finish(self, job_id: UUID, status: str, error: str = "") -> None`
    - `async def set_running(self, job_id: UUID) -> None`

`SerperMapPage` lives in this module so `app/services/serper/maps/plan.py` can import it without a cycle.

- [ ] **Step 1: Write the failing test**

```python
PAGES = [
    SerperMapPage("plumbers", 1, 0, 0, 30.1, -97.7, 13),
    SerperMapPage("plumbers", 2, 0, 0, 30.1, -97.7, 13),
    SerperMapPage("plumbers", 1, 1, 0, 30.2, -97.6, 13),
]


async def test_create_job_writes_one_request_row_per_page(db):
    repo = SerperMapRepository()
    info = await repo.create_job("plumbers @ Austin", {"city": "Austin"}, "k1", PAGES, 4)
    assert info.total_requests == 3
    assert info.pending_requests == 3
    assert info.status == "running"
    assert await SerperMapRequestModel.filter(job_id=info.id).count() == 3


async def test_find_unfinished_matches_the_config_key_only(db):
    repo = SerperMapRepository()
    job = await repo.create_job("j", {}, "k1", PAGES[:1], 1)
    assert (await repo.find_unfinished("k1")).id == job.id
    assert await repo.find_unfinished("other") is None


async def test_find_unfinished_skips_finished_and_failed_jobs(db):
    repo = SerperMapRepository()
    finished = await repo.create_job("j", {}, "k1", PAGES[:1], 1)
    await repo.finish(finished.id, SerperMapJobStatus.FINISHED)
    assert await repo.find_unfinished("k1") is None

    failed = await repo.create_job("j", {}, "k2", PAGES[:1], 1)
    await repo.finish(failed.id, SerperMapJobStatus.FAILED, "boom")
    assert await repo.find_unfinished("k2") is None


async def test_find_unfinished_returns_the_newest(db):
    repo = SerperMapRepository()
    await repo.create_job("old", {}, "k1", PAGES[:1], 1)
    newest = await repo.create_job("new", {}, "k1", PAGES[:1], 1)
    assert (await repo.find_unfinished("k1")).id == newest.id


async def test_pending_pages_returns_pending_and_failed_and_keeps_order(db):
    repo = SerperMapRepository()
    job = await repo.create_job("j", {}, "k1", PAGES, 1)
    await repo.mark_done(job.id, PAGES[0], http_status=200, places=20, credits=3, key_hint=0, saved=18)
    await repo.mark_failed(job.id, PAGES[1], http_status=500, error="boom")
    pages = await repo.pending_pages(job.id)
    assert pages == [PAGES[1], PAGES[2]]


async def test_mark_done_updates_the_job_counters(db):
    repo = SerperMapRepository()
    job = await repo.create_job("j", {}, "k1", PAGES, 1)
    await repo.mark_done(job.id, PAGES[0], http_status=200, places=20, credits=3, key_hint=0, saved=7)
    stored = await repo.get(job.id)
    assert stored.done_requests == 1
    assert stored.places_found == 20
    assert stored.places_saved == 7
    assert stored.credits_spent == 3
    assert stored.pending_requests == 2


async def test_mark_attempt_counts_calls_and_remembers_the_key_index(db):
    repo = SerperMapRepository()
    job = await repo.create_job("j", {}, "k1", PAGES[:1], 2)
    await repo.mark_attempt(job.id, PAGES[0], 1)
    request = await SerperMapRequestModel.get(job_id=job.id)
    assert request.attempts == 1 and request.key_hint == 1


async def test_finish_sets_status_and_error(db):
    repo = SerperMapRepository()
    job = await repo.create_job("j", {}, "k1", PAGES[:1], 1)
    await repo.finish(job.id, SerperMapJobStatus.EXHAUSTED, "all keys are out of credits")
    stored = await repo.get(job.id)
    assert stored.status == "exhausted"
    assert stored.error == "all keys are out of credits"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/repositories/test_serper_map_repository.py -v`
Expected: FAIL — `ImportError`

- [ ] **Step 3: Implement `SerperMapRepository`**

Bulk-create the request rows with `SerperMapRequestModel.bulk_create`, keying each row by `page` fields. Counter updates in `mark_done` use `F` expressions (`SerperMapJobModel.filter(id=...).update(done_requests=F("done_requests") + 1, ...)`) so concurrent keys never lose an increment. `find_unfinished` filters `config_key` and `status__in=[RUNNING, EXHAUSTED, INTERRUPTED]`, orders by `-created_at`. `_to_info` computes `pending_requests` as `total - done - failed`. Add `SerperMapRepository` to `app/repositories/__init__.py`.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/repositories/test_serper_map_repository.py -v`
Expected: PASS (8 passed)

- [ ] **Step 5: Commit**

```bash
git add app/repositories/serper_map_repository.py app/repositories/__init__.py tests/repositories/test_serper_map_repository.py
git commit -m "feat(serper): add the job repository"
```

---

### Task 3: Typed response types, errors, and the HTTP client

**Files:**
- Modify: `app/services/serper/maps/serper_map_type.py` (replace the body)
- Delete: `app/services/serper/maps/serper_map_api.py`
- Create: `app/services/serper/maps/serper_map_errors.py`
- Create: `app/services/serper/maps/serper_map_client.py`
- Create: `app/services/serper/maps/__init__.py` content (exports)
- Test: `tests/services/serper_maps/test_client.py`

**Interfaces:**
- Consumes: `httpx`.
- Produces:
  - `Place` dataclass with a `from_api` builder, keeping Serper's field names (`placeId`, `phoneNumber`, `ratingCount`, `openingHours`, `thumbnailUrl`, `bookingLinks`, `isUnclaimedListing`, `fid`, `cid`, `types`, `type`, `position`, `title`, `latitude`, `longitude`, `rating`, `address`, `website`).
  - `SearchResult(search_parameters: dict, ll: str, places: list[Place], credits: int)` with `from_api`.
  - Errors in `serper_map_errors.py`: `SerperError`, `SerperNoCreditsError`, `SerperAuthError`, `SerperRateLimitedError(retry_after: float | None)`, `SerperResponseError(status: int, message: str)`, `SerperUnavailableError`.
  - `SerperMapClient(api_key: str, *, timeout: float = 20.0, transport: httpx.AsyncBaseTransport | None = None)` with `async def search(self, keyword: str, lat: float, lon: float, zoom: int, page: int) -> SearchResult`, `async def balance(self) -> int`, `async def aclose(self)`.

- [ ] **Step 1: Write the failing tests**

```python
def _transport(status: int, body: dict, headers: dict | None = None):
    return httpx.MockTransport(
        lambda request: httpx.Response(status, json=body, headers=headers or {})
    )


async def test_search_sends_the_keyword_ll_and_page():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["key"] = request.headers["X-API-KEY"]
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"places": [], "credits": 3, "ll": "@30.1,-97.7,13z"})

    client = SerperMapClient("k1", transport=httpx.MockTransport(handler))
    await client.search("plumbers", 30.1, -97.7, 13, 2)
    assert seen["url"] == "https://google.serper.dev/maps"
    assert seen["key"] == "k1"
    assert seen["body"] == {"q": "plumbers", "ll": "@30.1,-97.7,13z", "page": 2}
    await client.aclose()


async def test_search_returns_typed_places():
    body = {"places": [{"position": 1, "title": "Cafe", "latitude": 30.1, "longitude": -97.7,
                        "rating": 4.5, "ratingCount": 12, "type": "Cafe", "types": ["Cafe"],
                        "cid": "9", "fid": "0x1:0x2", "placeId": "p1",
                        "phoneNumber": "(512) 555-0100", "website": "https://a.com",
                        "openingHours": {"Monday": "9 AM-5 PM"}}],
            "credits": 3, "ll": "@30.1,-97.7,13z"}
    client = SerperMapClient("k1", transport=_transport(200, body))
    result = await client.search("plumbers", 30.1, -97.7, 13, 1)
    place = result.places[0]
    assert isinstance(place, Place)
    assert place.placeId == "p1" and place.phoneNumber == "(512) 555-0100"
    assert place.openingHours.Monday == "9 AM-5 PM"
    assert place.openingHours.Tuesday is None
    assert result.credits == 3
    await client.aclose()


async def test_a_place_missing_optional_fields_still_builds():
    body = {"places": [{"position": 1, "title": "Bare"}], "credits": 3}
    client = SerperMapClient("k1", transport=_transport(200, body))
    place = (await client.search("x", 30.0, -97.0, 13, 1)).places[0]
    assert place.website is None and place.types == [] and place.openingHours is None
    await client.aclose()


@pytest.mark.parametrize("status,message,error", [
    (402, "Payment required", SerperNoCreditsError),
    (403, "Unauthorized.", SerperAuthError),
    (403, "Your plan has no credits left", SerperNoCreditsError),
    (403, "quota exceeded", SerperNoCreditsError),
    (401, "Unauthorized", SerperAuthError),
    (400, "Bad query", SerperResponseError),
    (500, "Server error", SerperUnavailableError),
    (503, "Unavailable", SerperUnavailableError),
])
async def test_http_failures_raise_typed_errors(status, message, error):
    client = SerperMapClient("k1", transport=_transport(status, {"message": message}))
    with pytest.raises(error) as info:
        await client.search("x", 30.0, -97.0, 13, 1)
    assert str(status) in str(info.value) or message in str(info.value)
    await client.aclose()


async def test_429_carries_the_retry_after():
    client = SerperMapClient("k1", transport=_transport(429, {"message": "slow down"}, {"Retry-After": "7"}))
    with pytest.raises(SerperRateLimitedError) as info:
        await client.search("x", 30.0, -97.0, 13, 1)
    assert info.value.retry_after == 7.0
    await client.aclose()


async def test_balance_reads_the_account_balance():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        return httpx.Response(200, json={"balance": 1554, "rateLimit": 5})

    client = SerperMapClient("k1", transport=httpx.MockTransport(handler))
    assert await client.balance() == 1554
    assert seen["url"] == "https://google.serper.dev/account"
    await client.aclose()


async def test_no_place_is_ever_a_raw_dict():
    body = {"places": [{"position": 2, "title": "X"}], "credits": 3}
    client = SerperMapClient("k1", transport=_transport(200, body))
    result = await client.search("x", 30.0, -97.0, 13, 1)
    assert all(isinstance(place, Place) for place in result.places)
    await client.aclose()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/services/serper_maps/test_client.py -v`
Expected: FAIL — `ImportError: cannot import name 'SerperMapClient'`

- [ ] **Step 3: Implement the types, errors, and client**

- `serper_map_type.py`: keep the field names the partner wrote. Give every optional field a default. `Place.from_api(data)` copies only known keys (`{k: v for k, v in data.items() if k in field_names}`) and turns `openingHours` into `OpeningHours.from_api(d)` (only the seven weekday keys). `SearchResult.from_api` builds typed places.
- `serper_map_client.py`: one module-level `MAPS_URL = "https://google.serper.dev/maps"` and `ACCOUNT_URL = "https://google.serper.dev/account"`. The constructor owns an `httpx.AsyncClient(base_url=None, timeout=timeout, transport=transport)`; `search` posts `{"q", "ll", "page"}` where `ll = f"@{lat},{lon},{zoom}z"`. Error mapping, in order: 200 → `SearchResult.from_api`; 429 → `SerperRateLimitedError` with `float(Retry-After)` when parsable; 402 → no credits; 401 or (403 whose lowercased body contains "unauthorized" or "invalid") → auth; 403 whose body mentions credit/quota/plan/limit → no credits; other 403 → auth; 5xx or `httpx.TransportError` → unavailable; any other 4xx → response error. Never include the key in a message. No retries inside the client — retry policy belongs to the scheduler.
- Delete `serper_map_api.py` (`git rm`), since the client replaces it.
- `maps/__init__.py` exports the client, config, plan, service, and errors in later tasks; add each export as the task lands.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/services/serper_maps/test_client.py -v`
Expected: PASS (12 passed)

- [ ] **Step 5: Commit**

```bash
git rm app/services/serper/maps/serper_map_api.py
git add app/services/serper/maps/ tests/services/serper_maps/test_client.py
git commit -m "feat(serper): typed maps client and errors"
```

---

### Task 4: Config and the plan

**Files:**
- Create: `app/services/serper/maps/serper_map_config.py`
- Create: `app/services/serper/maps/serper_map_plan.py`
- Test: `tests/services/serper_maps/test_plan.py`

**Interfaces:**
- Consumes: `app.services.google_map_scraper.google_map.geo.get_geocode`, `build_grid_cells`, `app.services.google_map_scraper.scraper.parse_bounding_box`, and `SerperMapPage` from Task 2.
- Produces:
  - `SerperMapConfig` (pydantic, frozen): `keywords: tuple[str, ...]` (min 1), `city: str`, `state: str | None`, `country: str` (2 letters, lowercased), `depth: int = 1` (1..10), `grid: int = 1` (1..10), `parallel: int = 5` (1..20), `has_phone: bool = False`, `has_website: bool = False`. Properties: `place -> str` (`"Austin, TX, US"`), `name -> str` (`"plumbers @ Austin, TX, US"`), `config_key -> str` (sha256 of a canonical JSON of every field except `parallel`).
  - `SerperMapPlan` (frozen): `config: SerperMapConfig`, `center: tuple[float, float]`, `bounding_box: tuple[float, float, float, float] | None`, `pages: list[SerperMapPage]`. Properties `tile_count`, `page_count`, `estimated_credits` (`3 * len(pages)`).
  - `async def build_plan(config: SerperMapConfig) -> SerperMapPlan` — geocodes via `asyncio.to_thread(get_geocode, ...)`, raises `SerperGeocodeError` when no match.

- [ ] **Step 1: Write the failing tests**

```python
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


async def test_a_whole_city_is_one_tile_with_every_page(monkeypatch):
    monkeypatch.setattr(plan_module, "get_geocode", lambda *a, **k: ((30.26, -97.74), None))
    built = await build_plan(_config(depth=3))
    assert built.tile_count == 1
    assert [page.page for page in built.pages] == [1, 2, 3]
    assert {(page.lat, page.lng, page.zoom) for page in built.pages} == {(30.26, -97.74, 15)}
    assert built.estimated_credits == 9


async def test_a_grid_becomes_n_by_n_tiles_and_every_page_per_tile(monkeypatch):
    box = [str(v) for v in (30.09, 30.51, -97.93, -97.56)]
    monkeypatch.setattr(plan_module, "get_geocode", lambda *a, **k: ((30.26, -97.74), box))
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
    monkeypatch.setattr(plan_module, "get_geocode", lambda *a, **k: ((30.26, -97.74), box))
    built = await build_plan(_config(depth=1, grid=2, keywords=("a", "b")))
    for keyword in ("a", "b"):
        cells = {(p.grid_x, p.grid_y) for p in built.pages if p.keyword == keyword}
        assert cells == {(0, 0), (0, 1), (1, 0), (1, 1)}


async def test_no_geocode_raises(monkeypatch):
    monkeypatch.setattr(plan_module, "get_geocode", lambda *a, **k: None)
    with pytest.raises(SerperGeocodeError):
        await build_plan(_config())
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/services/serper_maps/test_plan.py -v`
Expected: FAIL — `ImportError`

- [ ] **Step 3: Implement the config and plan**

`serper_map_config.py` uses `ConfigDict(frozen=True, extra="forbid")`, `StringConstraints` for keywords/city/state (min length 1, stripped), a 2-letter country pattern lowercased. `config_key` hashes `model_dump(exclude={"parallel"}, mode="json")` with `json.dumps(..., sort_keys=True)`.

`serper_map_plan.py`: `build_plan` calls `get_geocode(config.place, with_bbox=True, country=config.country)` in `asyncio.to_thread`, raises `SerperGeocodeError(config.place)` when it returns `None`; when `grid == 1` builds one page per depth at the centre with `zoom=15`; otherwise `parse_bounding_box(config.place, raw_box)` → `build_grid_cells(box, grid)` and one `SerperMapPage` per `(keyword, cell, page)` with `grid_x = index % grid`, `grid_y = index // grid`. Pages are ordered keyword → grid cell → page so a shallow sweep covers the whole city first.

Add `class SerperGeocodeError(SerperError)` to `serper_map_errors.py`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/services/serper_maps/test_plan.py -v`
Expected: PASS (7 passed)

- [ ] **Step 5: Commit**

```bash
git add app/services/serper/maps/serper_map_config.py app/services/serper/maps/serper_map_plan.py app/services/serper/maps/serper_map_errors.py tests/services/serper_maps/test_plan.py
git commit -m "feat(serper): config and request plan"
```

---

### Task 5: Place to LeadRow mapping and filters

**Files:**
- Create: `app/services/serper/maps/serper_map_rows.py`
- Test: `tests/services/serper_maps/test_rows.py`

**Interfaces:**
- Consumes: `Place` (Task 3), `SerperMapConfig` (Task 4), `LeadRow` + `build_lead_row` shape from `app/services/google_map_scraper/scrape/`, `keep_lead`, `format_phone_numbers`, `dedupe_rows`.
- Produces:
  - `def place_to_row(place: Place, keyword: str) -> LeadRow`
  - `def places_to_rows(places: list[Place], keyword: str, config: SerperMapConfig) -> list[LeadRow]` — maps, formats phones for `config.country`, applies `keep_lead`, returns rows in input order.

- [ ] **Step 1: Write the failing tests**

```python
PLACE = Place(
    position=1, title="Lalo Plumbing", latitude=30.3058, longitude=-97.7432,
    rating=5.0, ratingCount=23, type="Plumber",
    types=["Plumber", "Drainage service"], cid="1178220673215833450",
    fid="0x8644cb2ec65763b7:0x1059e175b60d256a", placeId="ChIJt2NXxi7LRIYRaiUNtnXhWRA",
    address="3800 N Lamar Blvd #200, Austin, TX 78756",
    website="https://laloplumbing.com/", phoneNumber="(737) 300-9817",
    openingHours=OpeningHours(Monday="Open 24 hours"),
    thumbnailUrl="https://lh3.googleusercontent.com/x",
)


def test_the_row_carries_every_dedupe_key_and_field():
    row = place_to_row(PLACE, "plumbers")
    assert row["title"] == "Lalo Plumbing"
    assert row["place_id"] == "ChIJt2NXxi7LRIYRaiUNtnXhWRA"
    assert row["data_id"] == "0x8644cb2ec65763b7:0x1059e175b60d256a"
    assert row["cid"] == "1178220673215833450"
    assert row["review_count"] == "23" and row["review_rating"] == "5.0"
    assert row["categories"] == "Plumber | Drainage service"
    assert row["descriptions"] == "Plumber"
    assert row["input_id"] == "plumbers"
    assert row["latitude"] == 30.3058 and row["longitude"] == -97.7432


def test_the_phone_is_formatted_for_the_country():
    row = place_to_row(PLACE, "plumbers")
    from app.services.google_map_scraper.scrape.phone_numbers import format_phone_numbers
    format_phone_numbers([row], "us")
    assert row["phone"] == "+17373009817"


def test_the_link_points_at_the_place():
    row = place_to_row(PLACE, "plumbers")
    assert row["link"].startswith("https://www.google.com/maps/search/?api=1&query=")
    assert "query_place_id=ChIJt2NXxi7LRIYRaiUNtnXhWRA" in row["link"]


def test_open_hours_round_trip_as_json():
    row = place_to_row(PLACE, "plumbers")
    assert json.loads(row["open_hours"]) == {"Monday": "Open 24 hours"}


def test_a_place_without_a_website_or_phone_gets_blank_cells():
    bare = Place(position=1, title="Bare", latitude=1.0, longitude=2.0)
    row = place_to_row(bare, "x")
    assert row["website"] == "" and (row["phone"] in ("", None))
    assert row["categories"] == "" and row["open_hours"] == ""


def test_has_phone_drops_rows_without_a_usable_number():
    bare = Place(position=1, title="Bare", latitude=1.0, longitude=2.0)
    kept = places_to_rows([PLACE, bare], "plumbers", _config(has_phone=True))
    assert [row["title"] for row in kept] == ["Lalo Plumbing"]


def test_has_website_drops_rows_without_a_website():
    bare = Place(position=1, title="Bare", latitude=1.0, longitude=2.0)
    kept = places_to_rows([PLACE, bare], "plumbers", _config(has_website=True))
    assert [row["title"] for row in kept] == ["Lalo Plumbing"]


def test_filters_off_keep_everything():
    bare = Place(position=1, title="Bare", latitude=1.0, longitude=2.0)
    assert len(places_to_rows([PLACE, bare], "plumbers", _config())) == 2


def test_the_mapped_row_satisfies_the_repository_contract(db):
    from app.repositories import GoogleMapProfileRepository, LeadRepository
    row = place_to_row(PLACE, "plumbers")
    repo = GoogleMapProfileRepository(LeadRepository())
    await repo.save_row(row, "s1")
    await repo.save_row(row, "s1")
    from app.models import GoogleMapProfileModel
    assert await GoogleMapProfileModel.filter(place_id=PLACE.placeId).count() == 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/services/serper_maps/test_rows.py -v`
Expected: FAIL — `ImportError`

- [ ] **Step 3: Implement the mapping**

Build the row as a `LeadRow` literal with every column of `COLUMNS`, the same columns `build_lead_row` fills, so the repository sees the shape it expects. Reuse `maps_place_url`, `encode_plus_code` from `lead_rows.py`/`extract_details.py`. `open_hours` is `json.dumps(dataclasses.asdict(place.openingHours))` when present else `""`. `categories` joins `types` with `" | "`. Numbers are stringified with `str(...)`; absent values are `""` except `latitude`/`longitude` which are floats or `None`. `places_to_rows` builds a `ScrapeSettings(keywords=(config.keywords[0],), has_phone=config.has_phone, has_website=config.has_website)` once and uses `keep_lead`; it formats phones with `format_phone_numbers(rows, config.country)` **before** filtering, so an unparsable number is dropped rather than counted as a phone.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/services/serper_maps/test_rows.py -v`
Expected: PASS (9 passed)

- [ ] **Step 5: Commit**

```bash
git add app/services/serper/maps/serper_map_rows.py tests/services/serper_maps/test_rows.py
git commit -m "feat(serper): map places to lead rows"
```

---

### Task 6: The key pool and the parallel drain

**Files:**
- Create: `app/services/serper/maps/serper_map_state.py`
- Test: `tests/services/serper_maps/test_scheduler.py`

**Interfaces:**
- Consumes: `SerperMapClient` (Task 3), `SerperMapPage` (Task 2), the error types (Task 3).
- Produces:
  - `SerperMapEvent` (frozen dataclass): `kind: str`, `page: SerperMapPage | None = None`, `key_index: int | None = None`, `detail: str = ""`, `done: int | None = None`, `total: int | None = None`.
  - `KeySlot` with `index: int`, `state: str` (`ready`/`dry`/`dead`), `async def search(...)`, `async def remaining(self) -> int`.
  - `class SerperMapScheduler(pages: list[SerperMapPage], clients: list[SerperMapClient], *, parallel: int, rest_seconds: float = 1.5, max_rate_limit_retries: int = 3, on_event: Callable[[SerperMapEvent], None] | None = None)` with `async def run(self, handle: PageHandler) -> SerperMapRunSummary`, where `PageHandler` is `Callable[[SerperMapPage, SearchResult, int], Awaitable[int]]` (page, result, key_index) → rows saved.
  - `SerperMapRunSummary` (frozen): `done: int`, `failed: int`, `pending: int`, `exhausted: bool`, `dead_keys: int`.

- [ ] **Step 1: Write the failing tests**

```python
class FakeClient:
    """A client whose search() records calls and answers from a script."""

    def __init__(self, index, answers):
        self.index = index
        self.answers = list(answers)
        self.calls = []

    async def search(self, keyword, lat, lon, zoom, page):
        self.calls.append((keyword, page))
        answer = self.answers.pop(0) if self.answers else SearchResult({}, "", [], 3)
        if isinstance(answer, Exception):
            raise answer
        return answer

    async def balance(self):
        return 100

    async def aclose(self):
        pass


def _pages(n):
    return [SerperMapPage("k", page, 0, 0, 30.0, -97.0, 13) for page in range(1, n + 1)]


async def test_every_page_is_handled_once():
    handled = []

    async def handle(page, result, key):
        handled.append(page.page)
        return 1

    summary = await SerperMapScheduler(_pages(6), [FakeClient(0, [])], parallel=2).run(handle)
    assert sorted(handled) == [1, 2, 3, 4, 5, 6]
    assert summary.done == 6 and summary.failed == 0 and summary.pending == 0


async def test_each_key_runs_parallel_requests_at_a_time():
    active = {0: 0, 1: 0}
    peak = {0: 0, 1: 0}

    class Slow(FakeClient):
        async def search(self, keyword, lat, lon, zoom, page):
            active[self.index] += 1
            peak[self.index] = max(peak[self.index], active[self.index])
            await asyncio.sleep(0.02)
            active[self.index] -= 1
            return SearchResult({}, "", [], 3)

    async def handle(page, result, key):
        return 0

    await SerperMapScheduler(
        _pages(20), [Slow(0, []), Slow(1, [])], parallel=3, rest_seconds=0
    ).run(handle)
    assert peak == {0: 3, 1: 3}


async def test_one_dry_key_hands_its_work_to_the_others():
    dry = FakeClient(0, [SerperNoCreditsError("out")] * 10)
    healthy = FakeClient(1, [])
    handled = []

    async def handle(page, result, key):
        handled.append(key)
        return 0

    summary = await SerperMapScheduler(
        _pages(10), [dry, healthy], parallel=2, rest_seconds=0
    ).run(handle)
    assert summary.done == 10
    assert dry.calls[0] == ("k", 1)
    assert sum(1 for key in handled if key == 0) <= 2  # only the in-flight ones
    assert summary.exhausted is False


async def test_all_keys_dry_stops_with_pending_work_and_is_not_a_failure():
    dry = [FakeClient(i, [SerperNoCreditsError("out")] * 10) for i in range(2)]

    async def handle(page, result, key):
        return 0

    summary = await SerperMapScheduler(_pages(10), dry, parallel=2, rest_seconds=0).run(handle)
    assert summary.exhausted is True
    assert summary.done == 0
    assert summary.pending == 10
    assert summary.failed == 0


async def test_an_auth_error_retires_the_key():
    bad = FakeClient(0, [SerperAuthError("403: Unauthorized")] * 5)
    good = FakeClient(1, [])

    async def handle(page, result, key):
        return 0

    summary = await SerperMapScheduler(_pages(4), [bad, good], parallel=1, rest_seconds=0).run(handle)
    assert summary.done == 4
    assert len(bad.calls) == 1
    assert summary.dead_keys == 1


async def test_a_rate_limited_key_waits_and_retries_itself():
    limited = FakeClient(0, [SerperRateLimitedError(retry_after=0)])
    healthy = FakeClient(1, [])

    async def handle(page, result, key):
        return 0

    summary = await SerperMapScheduler(
        _pages(2), [limited, healthy], parallel=1, rest_seconds=0
    ).run(handle)
    assert summary.done == 2
    assert limited.state == "ready"


async def test_repeated_rate_limits_retire_the_key():
    limited = FakeClient(0, [SerperRateLimitedError(retry_after=0)] * 5)
    healthy = FakeClient(1, [])

    async def handle(page, result, key):
        return 0

    summary = await SerperMapScheduler(
        _pages(4), [limited, healthy], parallel=1, rest_seconds=0
    ).run(handle)
    assert summary.done == 4
    assert limited.state == "dead"


async def test_a_transient_failure_is_retried_then_marked_failed():
    flaky = FakeClient(0, [SerperUnavailableError("500")] * 3)

    async def handle(page, result, key):
        return 0

    summary = await SerperMapScheduler(_pages(1), [flaky], parallel=1, rest_seconds=0).run(handle)
    assert summary.failed == 1
    assert summary.done == 0


async def test_a_page_that_raises_in_the_handler_does_not_stop_the_run():
    calls = []

    async def handle(page, result, key):
        calls.append(page.page)
        if page.page == 1:
            raise RuntimeError("save failed")
        return 1

    summary = await SerperMapScheduler(_pages(3), [FakeClient(0, [])], parallel=1, rest_seconds=0).run(handle)
    assert sorted(calls) == [1, 2, 3]
    assert summary.done == 2 and summary.failed == 1


async def test_the_rest_happens_between_batches():
    slept = []

    class Timed(FakeClient):
        async def search(self, keyword, lat, lon, zoom, page):
            return SearchResult({}, "", [], 3)

    async def fake_sleep(seconds):
        slept.append(seconds)

    async def handle(page, result, key):
        return 0

    scheduler = SerperMapScheduler(_pages(6), [Timed(0, [])], parallel=2, rest_seconds=1.5)
    scheduler._sleep = fake_sleep
    await scheduler.run(handle)
    assert slept == [1.5, 1.5]  # a rest after each full batch, before the next


async def test_events_report_progress():
    events = []

    async def handle(page, result, key):
        return 2

    await SerperMapScheduler(
        _pages(2), [FakeClient(0, [])], parallel=1, rest_seconds=0, on_event=events.append
    ).run(handle)
    kinds = [event.kind for event in events]
    assert "request_done" in kinds and kinds[-1] == "finished"
    assert events[-1].done == 2 and events[-1].total == 2
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/services/serper_maps/test_scheduler.py -v`
Expected: FAIL — `ImportError`

- [ ] **Step 3: Implement the scheduler**

Use an `asyncio.Queue` preloaded with every page, one `asyncio.Task` per `KeySlot`, and an `asyncio.Semaphore(parallel)` inside each slot's loop so a key never exceeds its own concurrency. The shared loop shape per key:

1. If the slot is not `ready`, exit.
2. Take a page from the queue without blocking forever (`get_nowait`, exit when empty).
3. `x-ratelimit-remaining` awareness is optional; the 1.5 s rest is the primary pacing.
4. Call `slot.search(...)` inside the semaphore; on success call `handle(page, result, key_index)`, count `done` or `failed` (a handler exception is a `failed` page, logged, run continues).
5. On `SerperNoCreditsError`: mark the slot `dry`, put the page back with `queue.put`, emit `key_dry`, and loop to exit.
6. On `SerperAuthError`: mark the slot `dead`, put the page back, emit `key_dead`, exit.
7. On `SerperRateLimitedError`: sleep `retry_after` (default 2 s) and retry the same page on the same key, up to `max_rate_limit_retries`; then mark `dead` and put the page back.
8. On `SerperUnavailableError`/`SerperResponseError`: retry twice with a short backoff, then count the page `failed` and continue (a `failed` page stops nothing).
9. Track the batch count and, after every `parallel` completed calls, `await self._sleep(rest_seconds)` before taking more work — that is the "1.5 s rest" and is what `test_the_rest_happens_between_batches` pins via the `_sleep` seam.

`run()` ends when the queue is empty or every slot is non-`ready`; it drains nothing after cancellation. `exhausted` is true when pages remain and every slot is `dry` or `dead`. `dead_keys` counts `dead` slots. Ensure the queue is never poisoned by a page put back while all keys are exiting (drain the queue into `pending` count at the end).

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/services/serper_maps/test_scheduler.py -v`
Expected: PASS (12 passed)

- [ ] **Step 5: Commit**

```bash
git add app/services/serper/maps/serper_map_state.py tests/services/serper_maps/test_scheduler.py
git commit -m "feat(serper): key pool and parallel drain"
```

---

### Task 7: The service facade

**Files:**
- Create: `app/services/serper/maps/serper_map_service.py`
- Test: `tests/services/serper_maps/test_service.py`

**Interfaces:**
- Consumes: everything above.
- Produces:
  - `SerperMapResult` (frozen): `job_id: UUID`, `status: str`, `requests_done: int`, `requests_failed: int`, `requests_pending: int`, `places_found: int`, `places_saved: int`, `credits_spent: int`, `error: str`.
  - `class SerperMapService(config, *, api_keys: Sequence[str], profiles: GoogleMapProfileRepository | None = None, crawlability: Callable[[list[LeadRow]], None] | None = None, on_event: Callable[[SerperMapEvent], None] | None = None, client_factory: Callable[[str], SerperMapClient] | None = None)`.
  - `async def plan(self) -> SerperMapPlan`
  - `async def find_unfinished(self) -> SerperMapJobInfo | None`
  - `async def start(self, plan: SerperMapPlan | None = None) -> UUID`
  - `async def resume(self, job_id: UUID) -> UUID`
  - `async def run(self, job_id: UUID) -> SerperMapResult`
  - `async def cancel(self) -> None`
  - `async def aclose(self)`

- [ ] **Step 1: Write the failing tests**

```python
def _service(keys=("k1",), answers=None, **overrides):
    return SerperMapService(
        _config(**overrides),
        api_keys=list(keys),
        client_factory=lambda key: FakeClient(list(keys).index(key), answers or []),
    )


async def test_start_writes_the_job_and_every_request(db, monkeypatch):
    monkeypatch.setattr(service_module, "build_plan", _fake_plan)
    service = _service()
    job_id = await service.start()
    info = await SerperMapRepository().get(job_id)
    assert info.total_requests == 4
    assert info.status == "running"
    await service.aclose()


async def test_run_saves_the_places_of_every_page(db, monkeypatch):
    monkeypatch.setattr(service_module, "build_plan", _fake_plan)
    service = _service(answers=_result_with_two_places())
    job_id = await service.start()
    result = await service.run(job_id)
    assert result.status == "finished"
    assert result.requests_done == 4
    assert result.places_found == 8
    assert result.places_saved == 8
    assert await LeadModel.all().count() == 2  # the same two places on every page
    await service.aclose()


async def test_a_second_run_does_not_duplicate_the_rows(db, monkeypatch):
    monkeypatch.setattr(service_module, "build_plan", _fake_plan)
    service = _service(answers=_result_with_two_places())
    job_id = await service.start()
    await service.run(job_id)
    second = await SerperMapService(
        _config(), api_keys=["k1"],
        client_factory=lambda key: FakeClient(0, _result_with_two_places()),
    )
    other_job = await second.start()
    await second.run(other_job)
    assert await LeadModel.all().count() == 2
    await service.aclose()
    await second.aclose()


async def test_known_keys_are_skipped(db, monkeypatch):
    monkeypatch.setattr(service_module, "build_plan", _fake_plan)
    first = _service(answers=_result_with_two_places())
    await first.run(await first.start())
    second = SerperMapService(
        _config(), api_keys=["k1"],
        known_keys=frozenset(await _all_keys()),
        client_factory=lambda key: FakeClient(0, _result_with_two_places()),
    )
    result = await second.run(await second.start())
    assert result.places_saved == 0
    assert await LeadModel.all().count() == 2
    await first.aclose()
    await second.aclose()


async def test_an_exhausted_run_is_not_failed_and_stays_pending(db, monkeypatch):
    monkeypatch.setattr(service_module, "build_plan", _fake_plan)
    service = _service(answers=[SerperNoCreditsError("out")] * 12)
    job_id = await service.start()
    result = await service.run(job_id)
    assert result.status == "exhausted"
    assert result.requests_pending == 4
    assert result.requests_failed == 0
    assert (await SerperMapRepository().get(job_id)).status == "exhausted"
    await service.aclose()


async def test_resume_skips_done_pages_and_retries_the_rest(db, monkeypatch):
    monkeypatch.setattr(service_module, "build_plan", _fake_plan)
    first = _service(answers=[SerperNoCreditsError("out")] * 12)
    job_id = await first.start()
    await first.run(job_id)
    resumed = _service(answers=_result_with_two_places())
    result = await resumed.run(job_id)
    assert result.requests_done == 4
    assert result.status == "finished"
    await first.aclose()
    await resumed.aclose()


async def test_find_unfinished_matches_the_same_config_only(db, monkeypatch):
    monkeypatch.setattr(service_module, "build_plan", _fake_plan)
    service = _service(answers=[SerperNoCreditsError("out")] * 12)
    job_id = await service.start()
    await service.run(job_id)
    assert (await service.find_unfinished()).id == job_id
    other = _service(depth=2)
    assert await other.find_unfinished() is None
    await service.aclose()
    await other.aclose()


async def test_cancel_marks_the_job_interrupted_and_keeps_progress(db, monkeypatch):
    monkeypatch.setattr(service_module, "build_plan", _fake_plan)
    service = _service(answers=_result_with_two_places())
    job_id = await service.start()
    task = asyncio.create_task(service.run(job_id))
    await asyncio.sleep(0)
    await service.cancel()
    result = await task
    assert result.status == "interrupted"
    info = await SerperMapRepository().get(job_id)
    assert info.status == "interrupted"
    assert info.done_requests >= 0
    await service.aclose()


async def test_the_crawlability_check_runs_only_with_has_website(db, monkeypatch):
    monkeypatch.setattr(service_module, "build_plan", _fake_plan)
    checked = []

    def crawl(rows):
        checked.append(len(rows))
        for row in rows:
            row["is_crawlable"] = True

    with_site = _service(answers=_result_with_two_places(), has_website=True)
    with_site._crawlability = crawl
    await with_site.run(await with_site.start())
    assert checked == [2, 2, 2, 2]
    assert await LeadModel.filter(is_crawlable=True).count() == 2

    checked.clear()
    without = _service(answers=_result_with_two_places())
    without._crawlability = crawl
    await without.run(await without.start())
    assert checked == []
    await with_site.aclose()
    await without.aclose()


async def test_no_api_key_is_a_typed_error(db, monkeypatch):
    monkeypatch.setattr(service_module, "build_plan", _fake_plan)
    with pytest.raises(SerperConfigError):
        _service(keys=())
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/services/serper_maps/test_service.py -v`
Expected: FAIL — `ImportError`

- [ ] **Step 3: Implement the facade**

- Constructor validates: `api_keys` non-empty after stripping and de-duplicating, each non-empty → else `SerperConfigError`; drops keys with `balance < 3` only in `run()` (needs async), emitting `key_dry`.
- `start()` calls `plan()` when none is passed, then `SerperMapRepository().create_job(...)` with `config.model_dump(mode="json")`, `config.config_key`, `plan.pages`, `len(api_keys)`.
- `run(job_id)`: load `pending_pages`, create one `SerperMapClient` per key through `client_factory`, build a `SerperMapScheduler`, and pass a handler that: maps rows with `places_to_rows`, dedupes with `dedupe_rows(rows, self._known)`, runs `self._crawlability(kept)` when `config.has_website`, saves with `GoogleMapProfileRepository.save_rows(kept, scrape_id=str(job_id))`, updates the in-memory known set with `lead_keys`, and calls `mark_done(..., saved=len(kept))`. A handler exception is caught by the scheduler and reported through `mark_failed`. `self._known` is loaded once from `profiles.key_rows()` when not injected.
- `cancel()` sets an `asyncio.Event` the scheduler observes between pages, then `finish(job_id, INTERRUPTED)`; `run()` also catches `asyncio.CancelledError`, marks `interrupted`, and re-raises after recording.
- `run()` maps the summary to a status: `exhausted` when `summary.exhausted`; `failed` when no page succeeded and every key is `dead`; `finished` when no pending pages remain; `interrupted` when cancelled.
- `aclose()` closes the clients.

Add `class SerperConfigError(SerperError)` to the errors module. Export `SerperMapService`, `SerperMapConfig`, `build_plan`, `SerperMapResult` from `app/services/serper/maps/__init__.py`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/services/serper_maps/test_service.py -v`
Expected: PASS (10 passed)

- [ ] **Step 5: Run the whole service suite**

Run: `uv run pytest tests/services/serper_maps tests/repositories/test_serper_map_repository.py tests/models/test_serper_map_model.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add app/services/serper/maps/serper_map_service.py app/services/serper/maps/__init__.py app/services/serper/maps/serper_map_errors.py tests/services/serper_maps/test_service.py
git commit -m "feat(serper): reusable maps scrape service"
```

---

### Task 8: The CLI

**Files:**
- Create: `cli/serper_map_scrape_cli.py`
- Modify: `pyproject.toml`
- Test: `tests/cli/test_serper_map_scrape_cli.py`

**Interfaces:**
- Consumes: `SerperMapService`, `SerperMapConfig`, the Serper error types, `SERPER_API_KEYS`.
- Produces: `build_parser()`, `read_config(argv=None) -> SerperMapConfig`, `load_api_keys() -> list[str]`, `main()`, and the console script `serper-map-scrape`.

- [ ] **Step 1: Write the failing tests**

```python
BASE = ["--keyword", "plumbers", "--city", "Austin", "--state", "TX", "--country", "us"]


def test_the_flags_build_the_config():
    config = read_config(BASE + ["--depth", "2", "--grid", "3", "--parallel", "7"])
    assert config.keywords == ("plumbers",)
    assert (config.depth, config.grid, config.parallel) == (2, 3, 7)


def test_repeated_keyword_flags_make_one_keyword_each():
    config = read_config(BASE + ["--keyword", "pizza"])
    assert config.keywords == ("plumbers", "pizza")


def test_defaults_are_one_page_one_tile_and_five_parallel():
    config = read_config(BASE)
    assert (config.depth, config.grid, config.parallel) == (1, 1, 5)


def test_filters_default_off_and_can_be_turned_on():
    assert read_config(BASE).has_phone is False
    on = read_config(BASE + ["--has-phone", "--has-website"])
    assert on.has_phone is True and on.has_website is True


def test_a_bad_depth_exits_with_the_flag_named(capsys):
    with pytest.raises(SystemExit) as info:
        read_config(BASE + ["--depth", "0"])
    assert "--depth" in str(info.value)


def test_a_missing_keyword_exits(capsys):
    with pytest.raises(SystemExit) as info:
        read_config(["--city", "Austin", "--country", "us"])
    assert "--keyword" in str(info.value)


def test_the_keys_are_read_from_the_environment(monkeypatch):
    monkeypatch.setenv("SERPER_API_KEYS", " k1 , k2 ,, k1 ")
    assert load_api_keys() == ["k1", "k2"]


def test_a_missing_key_list_exits_with_the_variable_named(monkeypatch):
    monkeypatch.delenv("SERPER_API_KEYS", raising=False)
    with pytest.raises(SystemExit) as info:
        load_api_keys()
    assert "SERPER_API_KEYS" in str(info.value)


def test_a_key_is_never_printed_in_the_plan(capsys, monkeypatch):
    monkeypatch.setenv("SERPER_API_KEYS", "secret-key-1")
    print_plan(_fake_plan(), ["secret-key-1"])
    printed = capsys.readouterr().out
    assert "secret-key-1" not in printed
    assert "2 key(s)" in printed or "1 key(s)" in printed


def test_the_resume_question_accepts_yes_and_defaults_to_yes(monkeypatch):
    monkeypatch.setattr("builtins.input", lambda prompt: "")
    assert ask_resume(_job_info()) is True
    monkeypatch.setattr("builtins.input", lambda prompt: "n")
    assert ask_resume(_job_info()) is False


def test_dry_run_writes_nothing(db, monkeypatch, capsys):
    monkeypatch.setattr(service_module, "build_plan", _fake_plan)
    monkeypatch.setenv("SERPER_API_KEYS", "k1")
    exit_code = main_with([*BASE, "--dry-run"])
    assert exit_code == 0
    assert await SerperMapJobModel.all().count() == 0
    assert "Dry run" in capsys.readouterr().out


def test_an_exhausted_run_exits_zero_and_says_so(db, monkeypatch, capsys):
    monkeypatch.setattr(service_module, "build_plan", _fake_plan)
    monkeypatch.setattr(cli, "SerperMapService", _ExhaustedService)
    monkeypatch.setenv("SERPER_API_KEYS", "k1")
    exit_code = main_with([*BASE, "--no-resume"])
    assert exit_code == 0
    assert "credits" in capsys.readouterr().out.lower()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/cli/test_serper_map_scrape_cli.py -v`
Expected: FAIL — `ImportError`

- [ ] **Step 3: Implement the CLI**

Argparse mirroring the existing CLI's style (grouped `filters`, `metavar`, help text naming the per-key meaning of `--parallel` and that `--depth` is Serper pages). `read_config` collects values, validates through `SerperMapConfig`, and on `ValidationError` prints one `✗ --flag: reason` line per problem (reuse the de-duplication pattern from `cli/google_map_scrape_cli.py`) and `sys.exit`. `load_api_keys` splits `SERPER_API_KEYS` on commas, strips, drops blanks and repeats, and exits naming the variable when empty.

`main()` flow: parse → `load_api_keys()` → `asyncio.run(_run(config, keys, args))`, which opens Tortoise with `TORTOISE_ORM` (the `with_database` helper pattern from the existing CLI) and:
1. builds the plan or prints it for `--dry-run` (tiles, pages, requests, key count, estimated credits) and returns 0;
2. unless `--no-resume`, asks `find_unfinished()` and prompts `Continue this job? [Y/n]`; yes resumes that job id, no starts a new one;
3. emits events to a single-line progress writer (`saved x/y, places n, credits c`), then prints the summary;
4. exits 0 for `finished` and `exhausted` (with a message saying the credits ran out and the job can be resumed), 130 for `interrupted`, 1 for `failed`.

Ctrl-C: catch `KeyboardInterrupt` around `asyncio.run` and print `✗ Stopped.` with exit 130; the service's cancel path has already marked the job.

Register the script in `pyproject.toml`:

```toml
[project.scripts]
google-map-scrape = "cli.google_map_scrape_cli:main"
serper-map-scrape = "cli.serper_map_scrape_cli:main"
crawler = "cli.crawler_cli:main"
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/cli/test_serper_map_scrape_cli.py -v`
Expected: PASS (12 passed)

- [ ] **Step 5: Commit**

```bash
git add cli/serper_map_scrape_cli.py pyproject.toml tests/cli/test_serper_map_scrape_cli.py
git commit -m "feat(cli): serper maps scrape command"
```

---

### Task 9: End-to-end check, docs, and the graph

**Files:**
- Modify: `docs/superpowers/specs/2026-11-22-serper-maps-cli-design.md` (status line only)
- Modify: `README.md` (one command example)
- Test: `tests/services/serper_maps/test_end_to_end.py`

**Interfaces:**
- Consumes: the CLI and the service.
- Produces: a documented command and a regression test proving the whole path.

- [ ] **Step 1: Write the failing end-to-end test**

```python
async def test_a_grid_run_through_the_service_stores_deduped_rows(db, monkeypatch):
    """18 requests, 4 keys, 5 parallel, one key dry: rows land once and the job finishes."""
    monkeypatch.setattr(service_module, "build_plan", _fake_plan_grid3_depth2)
    clients = {
        "k1": FakeClient(0, [SerperNoCreditsError("out")] * 30),
        "k2": FakeClient(1, _pages_of_places()),
        "k3": FakeClient(2, _pages_of_places()),
        "k4": FakeClient(3, _pages_of_places()),
    }
    service = SerperMapService(
        _config(depth=2, grid=3, keywords=("plumbers",), parallel=5),
        api_keys=list(clients),
        client_factory=lambda key: clients[key],
    )
    result = await service.run(await service.start())
    assert result.requests_done == 18
    assert result.status in ("finished", "exhausted")
    assert await LeadModel.all().count() == 20
    assert len(clients["k1"].calls) <= 5
    await service.aclose()


async def test_the_run_leaves_no_request_row_done_without_a_status(db, monkeypatch):
    ...  # after a run, every request is done or failed; none is pending unless exhausted
```

- [ ] **Step 2: Run it to verify it fails first**

Run: `uv run pytest tests/services/serper_maps/test_end_to_end.py -v`
Expected: FAIL until the fixtures are wired, then PASS.

- [ ] **Step 3: Add the README example and flip the spec status**

README gets a short section: the command, the `SERPER_API_KEYS` requirement, the 3-credit cost per page, and the resume behaviour.

Spec status line becomes `Status: implemented`.

- [ ] **Step 4: Run the full suite and the linters**

Run: `uv run pytest -q`
Expected: all pass, no regression in the existing browser-scraper tests.

Run: `uv run ruff check app cli tests && uv run pyright`
Expected: clean. Fix anything the new files introduce; do not silence rules project-wide.

- [ ] **Step 5: Refresh the knowledge graph**

Run: `graphify update .`
Expected: the new modules appear in `graphify-out/graph.json`; the dirty output is committed with the change.

- [ ] **Step 6: Commit**

```bash
git add README.md docs/superpowers/specs/2026-11-22-serper-maps-cli-design.md tests/services/serper_maps/test_end_to_end.py graphify-out
git commit -m "test(serper): end-to-end grid run, docs, and graph"
```

---

## Self-Review Notes

- **Spec coverage.** Service contract → Task 7 (facade + events + cancel). Key pool/pacing → Task 6. Storage → Tasks 1–2. Row mapping/filters/crawlable → Task 5 + Task 7. Resume → Tasks 2 + 7 + 8. Dry-run, exit codes → Task 8. Migration → Task 1. `serper_map_api.py` removal and typed `from_api` → Task 3. README/docs/graph → Task 9.
- **Review Focus mapping.** Dry key mid-batch → Task 6 `test_one_dry_key_hands_its_work_to_the_others`; all dry → Task 6 `test_all_keys_dry_stops...` and Task 7 `test_an_exhausted_run_is_not_failed...`; resume → Task 7 `test_resume_skips_done_pages...` and `test_a_second_run_does_not_duplicate_the_rows`; Ctrl-C → Task 7 `test_cancel_marks_the_job_interrupted...`; empty page → Task 6 handles it as a normal `SearchResult` with no places (the fake clients in Task 5 and Task 7 return them), plus Task 9's end-to-end assert.
- **Type consistency.** `SerperMapPage` is defined once (Task 2) and used by plan, scheduler, repository, service. `SearchResult`/`Place` defined in Task 3, consumed in 5/6/7. `SerperMapEvent` defined in Task 6, consumed in 7/8. Repository methods named in Task 2 are the ones the service calls in Task 7.
