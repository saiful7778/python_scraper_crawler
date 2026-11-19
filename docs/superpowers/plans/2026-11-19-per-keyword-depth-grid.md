# Per-keyword depth and grid Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let one CLI command give each keyword its own `--depth` and `--grid`, and delete deep scan end to end.

> **Amendment (after execution):** `--keywords` was removed; `--keyword`/`-k` is
> the only keyword flag. The tasks below describe the original plan, which kept a
> comma-list compatibility flag. See the spec for the current design.

**Architecture:** `PlaceQuery` gains a `keyword_plans` tuple of `KeywordPlan(keyword, depth, grid)`, derived from `keywords` plus the run-level `scroll_depth`/`grid` when no plans are given, so library callers and stored records keep working. The scraper reads a keyword's plan by its 1-based `keyword_index` instead of the run-level values, and treats `grid == 1` as the existing whole-city path. The CLI parses repeated `--keyword` flags into plans with two small argparse actions.

**Tech Stack:** Python 3.12, pydantic v2, argparse, pytest, fakeredis.

**Spec:** `docs/superpowers/specs/2026-11-19-per-keyword-depth-grid-design.md`

## Global Constraints

- Python 3.12, pydantic v2, `ruff`, `pyright` (standard), `pytest` with `asyncio_mode = "auto"`.
- No new dependencies.
- Field aliases on `PlaceQuery`/`ScrapeSettings` are stored record keys: add aliases, never repurpose one.
- Existing commands and tests keep their meaning: `--keywords a,b --depth 6 --grid 2` gives both keywords depth 6 and grid 2.
- Defaults per keyword: depth 5, grid 1.
- `grid == 1` is the whole-city path: one scrape, no bounding box, no `Grid`/`cell` lines.
- After the last task: `graphify update .`.

## Review Focus

- `--keyword pizza --depth 3` typed right before `--city`: the depth must attach to `pizza`, not leak to a later keyword or the run. Test in Task 4.
- `-k "3"`: a numeric keyword is a name, never a depth. Test in Task 4.
- A mixed run (`grid 1` and `grid 2` keywords): the whole-city keyword gets no tiles, no bbox filtering and no `cell` lines. Test in Task 2.
- `--depth 11` or `--grid 11` on one keyword: rejected with a message naming `--depth`/`--grid`, not `--keyword-plans`. Tests in Tasks 1 and 4.
- Changing one keyword's depth or grid: a different Redis queue, so an old run is not resumed with the new settings. Test in Task 1.

---

### Task 1: Per-keyword plans in the query model

**Files:**

- Modify: `app/services/google_map_scraper/place_query.py`
- Test: `tests/services/google_map_scraper/test_place_query.py` (create)

**Interfaces:**

- Consumes: `ScrapeSettings` (`keywords`, `scroll_depth` alias `depth`, `grid`, `has_phone`, `has_website`).
- Produces:
  - `class KeywordPlan(BaseModel)`: fields `keyword: CleanText`, `depth: int = 5` (ge 1, le 10, strict), `grid: int = 1` (ge 1, le 10, strict); frozen.
  - `PlaceQuery.keyword_plans: tuple[KeywordPlan, ...]` (field alias `plans`, `max_length=500`, default `()`).
  - `PlaceQuery.depth_of(keyword_index: int) -> int`
  - `PlaceQuery.grid_of(keyword_index: int) -> int`
  - `PlaceQuery.uses_grid` property `-> bool`

- [ ] **Step 1: Write the failing tests**

```python
# tests/services/google_map_scraper/test_place_query.py
import pytest
from pydantic import ValidationError

from app.services.google_map_scraper.place_query import PlaceQuery
from app.services.google_map_scraper.place_queue import run_id


def plans_of(**data) -> list[tuple[str, int, int]]:
    query = PlaceQuery.model_validate({"city": "Austin", "country": "us", **data})
    return [(p.keyword, p.depth, p.grid) for p in query.keyword_plans]


def test_plans_give_each_keyword_its_depth_and_grid():
    assert plans_of(
        plans=[
            {"keyword": "coffee shops", "depth": 6, "grid": 1},
            {"keyword": "pizza", "depth": 3, "grid": 2},
        ]
    ) == [("coffee shops", 6, 1), ("pizza", 3, 2)]


def test_plans_supply_the_keywords_and_are_read_by_one_based_index():
    query = PlaceQuery.model_validate(
        {
            "plans": [
                {"keyword": "coffee shops", "depth": 6},
                {"keyword": "pizza", "depth": 3, "grid": 2},
            ],
            "city": "Austin",
            "country": "us",
        }
    )
    assert query.keywords == ("coffee shops", "pizza")
    assert query.depth_of(1) == 6 and query.grid_of(1) == 1
    assert query.depth_of(2) == 3 and query.grid_of(2) == 2


def test_keywords_without_plans_get_depth_five_and_grid_one():
    assert plans_of(keywords=["cafes", "bakeries"]) == [
        ("cafes", 5, 1),
        ("bakeries", 5, 1),
    ]


def test_a_run_level_grid_applies_to_every_derived_plan():
    assert plans_of(keywords=["cafes", "bakeries"], grid=2) == [
        ("cafes", 5, 2),
        ("bakeries", 5, 2),
    ]


def test_uses_grid_is_true_only_when_a_plan_tiles():
    whole_city = PlaceQuery.model_validate(
        {"keywords": ["cafes"], "city": "Austin", "country": "us"}
    )
    tiled = PlaceQuery.model_validate(
        {
            "plans": [
                {"keyword": "cafes", "grid": 1},
                {"keyword": "pizza", "grid": 2},
            ],
            "city": "Austin",
            "country": "us",
        }
    )
    assert whole_city.uses_grid is False
    assert tiled.uses_grid is True


def test_a_changed_plan_changes_the_run_id():
    first = PlaceQuery.model_validate(
        {"plans": [{"keyword": "cafes", "depth": 5}], "city": "Austin", "country": "us"}
    )
    deeper = PlaceQuery.model_validate(
        {"plans": [{"keyword": "cafes", "depth": 6}], "city": "Austin", "country": "us"}
    )
    assert run_id(first) != run_id(deeper)


def test_a_depth_out_of_range_names_the_depth_field():
    with pytest.raises(ValidationError) as error:
        PlaceQuery.model_validate(
            {
                "plans": [{"keyword": "cafes", "depth": 11}],
                "city": "Austin",
                "country": "us",
            }
        )
    assert error.value.errors()[0]["loc"] == ("keyword_plans", 0, "depth")
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/services/google_map_scraper/test_place_query.py -q`
Expected: FAIL — `plans_of` returns `[]` or validation errors about unknown fields.

- [ ] **Step 3: Add `KeywordPlan`, `keyword_plans` and the helpers**

In `place_query.py`: import `BaseModel`, `ConfigDict`; define `KeywordPlan` next to `PlaceQuery`:

```python
class KeywordPlan(BaseModel):
    """One keyword and the effort its own search gets."""

    model_config = ConfigDict(frozen=True)

    keyword: CleanText
    depth: int = Field(default=5, ge=1, le=10, strict=True)
    grid: int = Field(default=1, ge=1, le=10, strict=True)
```

Add to `PlaceQuery`:

```python
keyword_plans: tuple[KeywordPlan, ...] = Field(
    default=(),
    alias="plans",
    max_length=500,
    description="Each keyword with its own depth and grid.",
)
```

Derive the two sides in the existing `mode="before"` validator (rename it
`_derive_fields` so it still reads as one step; do not add a second before-validator,
because pydantic runs those in reverse definition order): keep its `email`/`socials`
lines, and add — when `data` is a dict — either `data["keywords"]` from the plans'
keywords when `plans`/`keyword_plans` is present, or `data["keyword_plans"]` built
from `keywords` with `data.get("depth", data.get("scroll_depth"))` (fallback 5) and
`data.get("grid") or 1`. Leave a non-list `keywords` to field validation.

Add the accessors:

```python
def depth_of(self, keyword_index: int) -> int:
    """Batches the keyword at this 1-based index searches with."""
    return self.keyword_plans[keyword_index - 1].depth

def grid_of(self, keyword_index: int) -> int:
    """Grid side of the keyword at this 1-based index; 1 is the whole city."""
    return self.keyword_plans[keyword_index - 1].grid

@property
def uses_grid(self) -> bool:
    """True when any keyword splits the city into tiles."""
    return any(plan.grid > 1 for plan in self.keyword_plans)
```

Then change `engine_settings`'s guard from `if self.has_phone or self.has_website or self.grid:` to `if self.has_phone or self.has_website or self.uses_grid:`.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/services/google_map_scraper/test_place_query.py tests/services/google_map_scraper/test_place_queue.py tests/services/google_map_scraper/test_scraper.py tests/services/google_map_scraper/test_scraper_queue.py -q`
Expected: PASS (the new tests, plus every existing scraper test through the derived-plans path).

- [ ] **Step 5: Commit**

```bash
git add app/services/google_map_scraper/place_query.py tests/services/google_map_scraper/test_place_query.py
git commit -m "feat: give each keyword its own depth and grid in the place query"
```

---

### Task 2: The scraper runs each keyword with its own plan

**Files:**

- Modify: `app/services/google_map_scraper/scraper.py` (`_enqueue_run` ~1116-1181, `_queue_keyword` ~1183-1224, `_start_tile` ~736-768, `_summarize` ~1022-1031)
- Test: `tests/services/google_map_scraper/test_scraper_queue.py`

**Interfaces:**

- Consumes: `PlaceQuery.keyword_plans`, `depth_of`, `grid_of`, `uses_grid` (Task 1).
- Produces: tiles whose stored settings carry their keyword's `depth`; no new public names.

- [ ] **Step 1: Write the failing tests**

Add to `tests/services/google_map_scraper/test_scraper_queue.py`:

```python
def _planned(*plans, **overrides) -> PlaceQuery:
    data = {
        "plans": [
            {"keyword": keyword, "depth": depth, "grid": grid}
            for keyword, depth, grid in plans
        ],
        "city": "Austin",
        "country": "us",
        **overrides,
    }
    return PlaceQuery.model_validate(data)


def test_each_keyword_runs_with_its_own_grid(arrange, redis):
    env = arrange(lambda kw, slot: done(kw))
    PlaceScraper(redis).scrape(
        _planned(("cafes", 5, 1), ("pizza", 5, 2))
    )
    keywords = [kw for kw, _ in env.searched]
    assert keywords == ["cafes", "pizza", "pizza", "pizza", "pizza"]


def test_each_tile_carries_its_keywords_depth(arrange, redis):
    env = arrange(lambda kw, slot: done(kw))
    PlaceScraper(redis).scrape(
        _planned(("cafes", 4, 1), ("pizza", 6, 2))
    )
    depths = {
        env._keyword(sid): env.settings_of(sid)["depth"] for sid, _ in env.submitted
    }
    assert depths == {"cafes": 4, "pizza": 6}


def test_the_whole_city_keyword_of_a_mixed_run_prints_no_cell_lines(
    arrange, redis, capsys
):
    arrange(lambda kw, slot: done())
    PlaceScraper(redis).scrape(_planned(("cafes", 4, 1), ("pizza", 6, 2)))
    lines = _lines(capsys)
    assert "▶ Creating scrape: 1 keyword @ 1.0,2.0 depth=4" in lines
    assert "▶ Grid 2x2: 4 scrapes, one at a time, depth=6" in lines
    assert "1 unique rows, 1 after the city box and filters" not in lines
    assert "4 unique rows, 4 after the city box and filters" in lines


def test_a_whole_city_run_does_not_need_a_bounding_box(
    arrange, redis, monkeypatch
):
    asked = []

    def geocode(place, with_bbox=False, country=None):
        asked.append(with_bbox)
        return _geocode(place, with_bbox=with_bbox, country=country)

    monkeypatch.setattr(scraper_module, "get_geocode", geocode)
    arrange(lambda kw, slot: done(kw))
    PlaceScraper(redis).scrape(_planned(("cafes", 5, 1)))
    PlaceScraper(redis).scrape(_planned(("teas", 5, 2)))
    assert asked == [False, True]
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/services/google_map_scraper/test_scraper_queue.py -q -k "own_grid or keywords_depth or mixed_run"`
Expected: FAIL — four tiles for `cafes` (run-level grid is unset, so `cafes` gets one tile but `pizza` also gets one; depth prints 5).

- [ ] **Step 3: Read each keyword's plan in the scraper**

- `_enqueue_run`: use `if query.uses_grid:` where it decides whether to geocode with `with_bbox=True`.
- `_queue_keyword`: replace `query.grid` with the keyword's plan:

```python
plan = query.keyword_plans[keyword_index - 1]
base = cast(
    SettingsData,
    anchored.model_copy(
        update={"keywords": (keyword,), "proxies": (), "scroll_depth": plan.depth}
    ).model_dump(by_alias=True, mode="json"),
)
cells = (
    build_grid_cells(bounding_box, plan.grid)
    if plan.grid > 1 and bounding_box
    else None
)
```

- `_start_tile`: `depth = self._query.depth_of(keyword)` and `grid = self._query.grid_of(keyword)`; print `depth={depth}` in both the `▶ Creating scrape` and `▶ Grid {grid}x{grid}` lines (the latter is only reached for tiled keywords).
- `_summarize`: `if self._query.grid_of(keyword) < 2 or keyword in self._summarized: return`.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/services/google_map_scraper/ -q`
Expected: PASS, including the existing `grid=2` tests, which now go through derived plans.

- [ ] **Step 5: Commit**

```bash
git add app/services/google_map_scraper/scraper.py tests/services/google_map_scraper/test_scraper_queue.py
git commit -m "feat: run each keyword with its own depth and grid"
```

---

### Task 3: Remove deep scan end to end

**Files:**

- Modify: `app/services/google_map_scraper/place_query.py`
- Modify: `app/services/google_map_scraper/scraper.py`
- Modify: `app/services/google_map_scraper/google_map/geo.py:44` (`GridCell._sub`)
- Modify: `app/services/google_map_scraper/place_queue.py:162-190` (`insert_after_keyword`)
- Modify: `cli/google_map_scrape_cli.py` (the `--deep-scan` surface only)
- Test: `tests/services/google_map_scraper/test_scraper_queue.py` (delete three tests)
- Test: `tests/cli/test_google_map_scrape_cli.py` (add one test)
- Docs: `.opencode/skills/google-map-scrape-cli/SKILL.md`, `.opencode/skills/google-map-scrape-cli/reference.md`

**Interfaces:**

- Consumes: nothing new.
- Produces: no `deep_scan` field, no `SATURATED`, no `_split`, no `GridCell._sub`, no `PlaceTileQueue.insert_after_keyword`, no `--deep-scan` flag. `--keywords`, `--depth` and `--grid` stay run-level until Task 4.

- [ ] **Step 1: Write the failing CLI test**

Add to `tests/cli/test_google_map_scrape_cli.py`:

```python
def test_deep_scan_is_rejected(monkeypatch) -> None:
    from cli.google_map_scrape_cli import read_query

    monkeypatch.setattr(sys, "argv", ["google_map_scrape_cli.py", *BASE_ARGS, "--deep-scan"])
    with pytest.raises(SystemExit):
        read_query()
```

- [ ] **Step 2: Delete the three deep-scan tests**

Remove `test_deep_scan_splits_a_saturated_tile_once`, `test_the_no_proxy_deep_scan_prints_the_base_header_and_cap_line`, and `test_no_proxy_deep_scan_sub_tiles_finish_before_the_next_keyword` from `tests/services/google_map_scraper/test_scraper_queue.py`. They are deleted, not skipped. `pytest.raises(SystemExit)` on an unknown option comes from argparse's own `error()`, which exits before validation, so the new CLI test needs no other change.

- [ ] **Step 3: Remove the code**

- `place_query.py`: delete the `deep_scan` field and the `if data.get("deep_scan") is True and data.get("grid") is None: data["grid"] = 3` branch.
- `scraper.py`: delete `SATURATED`, the `deep_scan_note` in `_start_tile`, the `sub_tag` in both cell prints, the deep-scan `if` in `_handle`'s success path (the one checking `self._query.deep_scan ... len(rows) >= SATURATED`), and the whole `_split` method.
- `geo.py`: drop `_sub: NotRequired[bool]` from `GridCell`. Check whether `NotRequired` is still used there before removing the import.
- `place_queue.py`: delete `insert_after_keyword` (its only caller was `_split`).
- `cli/google_map_scrape_cli.py`: delete the `--deep-scan` argument, the three `query.deep_scan` uses in `print_plan` (the `tiles` note), the `logger.info` field, and the `if query.deep_scan:` estimate block in `main`.

- [ ] **Step 4: Update the skill docs**

In `.opencode/skills/google-map-scrape-cli/SKILL.md`: drop the `--deep-scan` row; drop "or deep scan" and the deep-scan sentence from the description and step 3; change the `--limit` row and the last two "Common mistakes" bullets to drop sub-tiles and deep scan; change the "Grid and deep-scan runs print per-cell lines" bullet to "Grid runs print per-cell lines". In `reference.md`: delete the `SATURATED` bullet, the deep-scan clause in the `--limit` bullet, the "Runtime far exceeds the deep-scan estimate" row, and the grid/deep-scan mention in "Large runs". Leave `docs/superpowers/specs/` and `docs/superpowers/plans/` history untouched.

- [ ] **Step 5: Grep for leftovers**

Run: `grep -rn "deep_scan\|deep-scan\|SATURATED\|insert_after_keyword\|_sub" app cli tests .opencode/skills/google-map-scrape-cli`
Expected: no matches.

- [ ] **Step 6: Run the suite**

Run: `uv run pytest tests/services/google_map_scraper/ tests/cli -q`
Expected: PASS, including the new `test_deep_scan_is_rejected`.

- [ ] **Step 7: Commit**

```bash
git add app/services/google_map_scraper cli/google_map_scrape_cli.py tests/services/google_map_scraper tests/cli .opencode/skills/google-map-scrape-cli
git commit -m "refactor: remove deep scan"
```

---

### Task 4: The CLI takes one plan per keyword

**Files:**

- Modify: `cli/google_map_scrape_cli.py`
- Test: `tests/cli/test_google_map_scrape_cli.py`

**Interfaces:**

- Consumes: `PlaceQuery.keyword_plans`, `depth_of`, `grid_of` (Task 1); tiles built from plans (Task 2).
- Produces: `namespace.keyword_plans` (list of `{"keyword": str, "depth": int | None, "grid": int | None}`) and `namespace.keyword_plan_error` (str | None) from argparse; `print_plan(query: CliQuery) -> None` printing one line per keyword.

- [ ] **Step 1: Write the failing tests**

Add to `tests/cli/test_google_map_scrape_cli.py`:

```python
def test_each_keyword_takes_the_depth_and_grid_that_follow_it(monkeypatch):
    from cli.google_map_scrape_cli import read_query

    args = [
        "--keyword", "coffee shops", "--depth", "6", "--grid", "1",
        "--keyword", "pizza", "--depth", "3", "--grid", "2",
        "--city", "Austin", "--state", "TX", "--country", "US",
    ]
    monkeypatch.setattr(sys, "argv", ["google_map_scrape_cli.py", *args])
    query = read_query()
    assert [(p.keyword, p.depth, p.grid) for p in query.keyword_plans] == [
        ("coffee shops", 6, 1),
        ("pizza", 3, 2),
    ]
    assert query.keywords == ("coffee shops", "pizza")


def test_a_keyword_without_flags_gets_depth_five_and_grid_one(monkeypatch):
    query = parse(monkeypatch, "-k", "pizza")
    # BASE_ARGS already opens "coffee shops" with no flags of its own
    assert [(p.keyword, p.depth, p.grid) for p in query.keyword_plans] == [
        ("coffee shops", 5, 1),
        ("pizza", 5, 1),
    ]


def test_a_keyword_list_shares_the_depth_and_grid_after_it(monkeypatch):
    from cli.google_map_scrape_cli import read_query

    args = [
        "--keywords", "coffee shops,bakeries", "--depth", "6", "--grid", "2",
        "--city", "Austin", "--country", "us",
    ]
    monkeypatch.setattr(sys, "argv", ["google_map_scrape_cli.py", *args])
    assert [(p.keyword, p.depth, p.grid) for p in read_query().keyword_plans] == [
        ("coffee shops", 6, 2),
        ("bakeries", 6, 2),
    ]


def test_a_numeric_keyword_is_a_name_not_a_depth(monkeypatch):
    from cli.google_map_scrape_cli import read_query

    monkeypatch.setattr(
        sys,
        "argv",
        ["google_map_scrape_cli.py", "-k", "3", "--city", "Austin", "--country", "us"],
    )
    assert [(p.keyword, p.depth) for p in read_query().keyword_plans] == [("3", 5)]


def test_a_depth_before_any_keyword_exits_with_a_named_error(monkeypatch):
    from cli.google_map_scrape_cli import read_query

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "google_map_scrape_cli.py", "--depth", "6", "-k", "pizza",
            "--city", "Austin", "--country", "us",
        ],
    )
    with pytest.raises(SystemExit) as exit_info:
        read_query()
    assert "--depth must follow a --keyword or --keywords flag" in str(exit_info.value)


def test_no_keyword_exits_with_the_required_message(monkeypatch):
    from cli.google_map_scrape_cli import read_query

    monkeypatch.setattr(
        sys, "argv", ["google_map_scrape_cli.py", "--city", "Austin", "--country", "us"]
    )
    with pytest.raises(SystemExit) as exit_info:
        read_query()
    assert "--keyword is required" in str(exit_info.value)


def test_a_depth_out_of_range_is_reported_against_the_depth_flag(monkeypatch):
    from cli.google_map_scrape_cli import read_query

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "google_map_scrape_cli.py", "-k", "pizza", "--depth", "11",
            "--city", "Austin", "--country", "us",
        ],
    )
    with pytest.raises(SystemExit) as exit_info:
        read_query()
    assert "✗ --depth:" in str(exit_info.value)


def test_dry_run_prints_one_line_per_keyword(monkeypatch, capsys):
    import cli.google_map_scrape_cli as cli

    args = [
        "--keyword", "coffee shops", "--depth", "6",
        "--keyword", "pizza", "--depth", "3", "--grid", "2",
        "--city", "Austin", "--country", "us", "--dry-run",
    ]
    monkeypatch.setattr(sys, "argv", ["google_map_scrape_cli.py", *args])
    cli.main()
    out = capsys.readouterr().out
    assert "  'coffee shops': depth 6, 1 scrape(s), whole city" in out
    assert "  'pizza': depth 3, 4 scrape(s), 2x2 grid" in out
    assert "Scrapes: 5" in out
```

Every error assertion here checks the `SystemExit` message, which is what `read_query` passes to `sys.exit`; the existing error tests in this file read `str(exit_info.value)` the same way.

- [ ] **Step 2: Run the CLI tests to verify they fail**

Run: `uv run pytest tests/cli/test_google_map_scrape_cli.py -q`
Expected: FAIL — `--keyword` is unrecognized, `--depth` is still run-level.

- [ ] **Step 3: Parse repeated keyword flags into plans**

Add two module-level `argparse.Action` subclasses above `build_parser`:

```python
class KeywordAction(argparse.Action):
    """Open a keyword plan: the --depth/--grid that follow belong to it."""

    def __call__(self, parser, namespace, values, option_string=None):
        plans = getattr(namespace, "keyword_plans", None)
        if plans is None:
            plans = namespace.keyword_plans = []
        names = [values] if isinstance(values, str) else list(values)
        plans.extend({"keyword": name, "depth": None, "grid": None} for name in names)


class KeywordScopedAction(argparse.Action):
    """Set a value on the plan the last keyword flag opened."""

    def __call__(self, parser, namespace, values, option_string=None):
        plans = getattr(namespace, "keyword_plans", None)
        if not plans:
            namespace.keyword_plan_error = (
                f"{option_string} must follow a --keyword or --keywords flag"
            )
            return
        plans[-1][self.dest] = values
```

In `build_parser`, replace the `--keywords` argument and the run-level `--depth`/`--grid` with:

```python
ap.add_argument(
    "--keyword", "-k",
    action=KeywordAction,
    metavar="KEYWORD",
    help='a business type to search for, e.g. "coffee shops"; repeat it for more keywords, and give each its own --depth/--grid after it',
)
ap.add_argument(
    "--keywords",
    type=keyword_names,
    action=KeywordAction,
    metavar="KW,...",
    help="comma-separated business types; all of them take the --depth/--grid that follow",
)
ap.add_argument(
    "--depth", type=int, action=KeywordScopedAction,
    metavar="N",
    help="batches of results per keyword, 1 to 10 (default: 5); follows a --keyword/--keywords flag",
)
ap.add_argument(
    "--grid", type=int, action=KeywordScopedAction,
    metavar="N",
    help="split the city into N x N map tiles for that keyword, 1 to 10 (default: 1); follows a --keyword/--keywords flag",
)
```

Delete the `--deep-scan` argument and its help text.

- [ ] **Step 4: Validate plans in `read_query`**

```python
def read_query() -> CliQuery:
    """Parse the command line into a validated query; exits with the reason if it is invalid."""
    values = {
        k: v for k, v in vars(build_parser().parse_args()).items() if v is not None
    }
    error = values.pop("keyword_plan_error", None)
    if error:
        logger.warning("invalid arguments: %s", error)
        sys.exit(f"✗ {error}")
    plans = values.pop("keyword_plans", None)
    if not plans:
        logger.warning("invalid arguments: no keyword")
        sys.exit(
            '✗ --keyword is required: give at least one keyword '
            '(--keyword "coffee shops" or --keywords "a,b").'
        )
    values["keyword_plans"] = [
        {k: v for k, v in plan.items() if v is not None} for plan in plans
    ]
    if values.get("proxies"):
        values["proxies"] = list(dict.fromkeys(values["proxies"]))
    try:
        return CliQuery.model_validate(values)
    except ValidationError as error:
        lines = []
        for problem in error.errors():
            field = next(
                (str(part) for part in reversed(problem["loc"]) if isinstance(part, str)),
                "",
            )
            flag = FLAG_NAMES.get(field, f"--{field.replace('_', '-')}")
            ...
```

The rest of the `except` body is unchanged. The reversed-path lookup is what makes a bad plan value read `✗ --depth: ...` instead of `✗ --keyword-plans: ...`.

`CliQuery` keeps its `scroll_depth`/`grid` fields (defaults 5 and None) so library callers and tests that build it directly still work; the CLI no longer passes them.

- [ ] **Step 5: Print the plan per keyword**

In `print_plan`, drop the `tiles`, `deep_scan` and run-level depth logic and print:

```python
place = ", ".join(
    part for part in (query.city, query.state, query.country.upper()) if part
)
print("Dry run: nothing will be scraped or written.\n")
keywords = ", ".join(repr(plan.keyword) for plan in query.keyword_plans)
print(f"Search: {keywords} in {place}")
for plan in query.keyword_plans:
    tiles = plan.grid**2
    where = "whole city" if plan.grid == 1 else f"{plan.grid}x{plan.grid} grid"
    print(f"  {plan.keyword!r}: depth {plan.depth}, {tiles} scrape(s), {where}")
print(f"Scrapes: {sum(plan.grid**2 for plan in query.keyword_plans)}")
print(f"Max time: {query.max_seconds}s per scrape")
print(f"Website: level {query.website_depth}")
print(f"Proxies: {len(query.proxies or [])}")
row_limit = f"{query.limit} per scrape" if query.limit else "none"
print(f"Row limit: {row_limit}")
filters = [
    name
    for name, on in (("phone", query.has_phone), ("website", query.has_website))
    if on
]
print(f"Filters: {', '.join(filters) or 'none'}")
```

- [ ] **Step 6: Update `main`'s log line, heads-up and deep-scan block**

- The `logger.info("query: ...")` line: replace `grid=%s deep_scan=%s` with `plans=%r` and pass `[(p.keyword, p.depth, p.grid) for p in query.keyword_plans]`.
- Delete the whole `if query.deep_scan:` estimate block.
- Replace the heads-up condition with
  `if any(plan.depth >= 10 or plan.grid >= 2 for plan in query.keyword_plans):`
  and leave the printed text as it is.

- [ ] **Step 7: Run the CLI tests and the linters**

Run: `uv run pytest tests/cli -q && uv run ruff check cli tests && uv run pyright cli/google_map_scrape_cli.py app/services/google_map_scraper`
Expected: PASS, no lint or type errors.

- [ ] **Step 8: Commit**

```bash
git add cli/google_map_scrape_cli.py tests/cli/test_google_map_scrape_cli.py
git commit -m "feat: take a depth and grid per keyword on the command line"
```

---

### Task 5: Update the skill docs' flag table and examples, then rebuild the graph

**Files:**

- Modify: `.opencode/skills/google-map-scrape-cli/SKILL.md`
- Modify: `.opencode/skills/google-map-scrape-cli/reference.md`

**Interfaces:**

- Consumes: the flag behavior from Task 4.
- Produces: docs only.

- [ ] **Step 1: Rewrite the flags the task changed**

In `SKILL.md`:

- Step 1 of the workflow: gather `--keyword`/`--keywords`, `--city`, `--country`; say each `--keyword` may be followed by its own `--depth`/`--grid`, and that leaving them out means depth 5 and grid 1.
- Examples: add one per-keyword example and keep the comma-list ones:

```bash
uv run --env-file .env google-map-scrape \
  --keyword "coffee shops" --depth 6 \
  --keyword "pizza" --depth 3 --grid 2 \
  --city Austin --state TX --country us --dry-run
```

- Flags table: replace the `--keywords` row with `--keyword`, `-k` (repeatable) and `--keywords` (comma list, still supported); change the `--depth` and `--grid` rows to say they follow a keyword flag and apply to that keyword only, defaults 5 and 1, and that `--grid 1` is the whole city; drop the `--deep-scan` row; update the `--limit` row.
- Troubleshooting row `Could not resolve a bounding box` with `--grid`: say it applies when any keyword has a grid of 2 or more.
- In `reference.md`, describe resume identity as "changing any flag, including one keyword's depth or grid".

- [ ] **Step 2: Check the docs against the code**

Run: `grep -n "deep" .opencode/skills/google-map-scrape-cli/*.md` and `uv run --env-file .env google-map-scrape --help 2>/dev/null || uv run python cli/google_map_scrape_cli.py --help`
Expected: no deep-scan mentions in the skill docs; `--help` lists `-k/--keyword`, `--keywords`, `--depth`, `--grid` and no `--deep-scan`.

- [ ] **Step 3: Run the full suite one last time**

Run: `uv run pytest -q`
Expected: PASS (except tests marked `postgres` skipped, as before).

- [ ] **Step 4: Rebuild the knowledge graph**

Run: `graphify update .`

- [ ] **Step 5: Commit**

```bash
git add .opencode/skills/google-map-scrape-cli graphify-out
git commit -m "docs: document per-keyword depth and grid"
```
