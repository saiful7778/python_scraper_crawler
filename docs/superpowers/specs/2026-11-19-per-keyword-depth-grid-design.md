# Per-keyword depth and grid, and the removal of deep scan

Date: 2026-11-19
Status: design, awaiting approval

## Intent

One command today gives every keyword the same depth and the same city grid: the
CLI parses a single `--depth` and a single `--grid`, `PlaceQuery` stores exactly
one of each, and `PlaceScraper` builds every keyword's tiles from those two
values. In practice different business types need different effort — "coffee
shops" in a dense downtown needs a deeper sweep and maybe a grid, while "pizza"
needs a shallow search — so the user ends up running the CLI several times, one
command per configuration, and loses the shared geocode, the shared browser, the
cross-keyword dedupe, and the single salvage list.

The partner wants to write one command:

```bash
mycli \
  --keyword "coffee shops" --depth 6 --grid 1 \
  --keyword "pizza"        --depth 3 --grid 2 \
  --city "Austin" --state TX --country US
```

and have each keyword searched with its own depth and grid, while proxies, the
city, the language, the time limit, the row limit and the filters stay shared by
the whole run. Deep scan goes away entirely in the same change.

## Goals

- Each keyword carries its own depth and its own grid, set by flags that follow
  that keyword on the command line.
- Omitted values fall back per keyword: depth 5, grid 1.
- One `scrape()` call per command: one geocode, one browser, one dedupe pass
  across keywords, one Ctrl-C salvage, one resume queue.
- `--proxies` is one list for the whole run, unchanged in meaning and in the
  number of parallel tiles.
- Remove deep scan: the CLI flag, the `deep_scan` field, the saturated-tile
  split, sub-tiles, `SATURATED`, `PlaceTileQueue.insert_after_keyword`, and the
  tests and docs that describe it.
- `--keyword` is the only keyword flag: `--keywords "a,b"` is removed, because
  a keyword list cannot give each name its own depth and grid.

## Non-goals

- Per-keyword proxies, language, `--max-time`, `--limit` or filters. Those stay
  run-wide, exactly as today.
- Per-keyword geocoding or per-keyword bounding boxes. One geocode per run; the
  box is shared by every keyword that tiles.
- Renaming the `keywords` field of `ScrapeSettings`, or changing stored scrape
  records, Redis tile shapes, or the Postgres tables.
- Any REST/API surface. No module under `app/http` builds a `PlaceQuery`; the
  stored-scrape path keeps using `ScrapeSettings` unchanged.

## Anti-goals (the work failed if any of these happen)

- A keyword silently searched with another keyword's depth or grid.
- `--depth` or `--grid` silently ignored when it appears before any keyword.
- A bare number after a keyword being read as a depth (`-k pizza 3`), so a typo
  becomes a configuration.
- Losing the shared-run behaviour: a command that used to geocode once, dedupe
  across keywords and salvage on Ctrl-C must still do all three.
- A removed `--keywords` flag being silently accepted, so a command that used
  it searches something other than what the user typed.
- Leaving a half-removed deep scan: dead fields, dead helpers, tests that pass
  because the feature is unreachable.

## Constraints

- Python 3.12, pydantic v2, `argparse`, `ruff`, `pyright` (standard), `pytest`
  with `asyncio_mode = "auto"`. Buffer/queue tests use `fakeredis`; no real
  browser, Redis or Postgres is needed for them.
- No new dependencies.
- Field aliases are stored record keys, so `PlaceQuery`/`ScrapeSettings` aliases
  may only be added, not repurposed.
- After the change, run `graphify update .` (required by `AGENTS.md`).

## Decisions

### D1. Command line

Flags only. A bare number after a keyword is a keyword name, never a depth, so a
mistake cannot be silently read as configuration.

- `--keyword KW` / `-k KW` (repeatable) starts one keyword entry with no explicit
  depth or grid. It is the only keyword flag; `--keywords` is gone.
- `--depth N` and `--grid N` apply to the entry opened by the most recent
  `--keyword`. Repeating them for the same entry keeps the last value.
- `--depth`/`--grid` before any keyword flag is a usage error that names the
  problem: `✗ --depth must follow a --keyword flag.`
- No keyword flag at all exits with
  `✗ --keyword is required: give at least one, e.g. --keyword "coffee shops".`
- `--depth` range 1–10 and `--grid` range 1–10, as today, enforced by the model
  (`strict=True`, so `--depth 6.5` is rejected).
- Other flags (`--city`, `--state`, `--country`, `--lang`,
  `--max-time`, `--limit`, `--proxies`, `--has-phone`, `--has-website`,
  `--dry-run`) are unchanged and run-wide.

Parsing is done by two small `argparse.Action` subclasses (`KeywordAction`,
`KeywordScopedAction`) because argparse cannot scope an optional flag to a
preceding flag on its own. The actions write `namespace.keyword_plans`, a list of
`{"keyword": str, "depth": int | None, "grid": int | None}`, and
`read_query` validates it through `CliQuery`.

### D2. `grid 1` means the whole-city path

A grid of 1 is the existing no-grid behaviour: exactly one scrape for that
keyword, no bounding box required, no `▶ Grid 1x1` line, no `cell 1/1` lines.
`PlaceQuery.grid` keeps `None | int` for library callers; the per-keyword plan
uses `int` with 1 as its default, and the scraper maps 1 to the no-tile path.
This keeps a keyword written without `--grid` byte-identical in behaviour to
today's no-grid run.

### D3. Query model

`place_query.py` gains:

```python
class KeywordPlan(BaseModel):
    keyword: CleanText
    depth: int = Field(default=5, ge=1, le=10, strict=True)
    grid: int = Field(default=1, ge=1, le=10, strict=True)
```

`PlaceQuery` gains `keyword_plans: tuple[KeywordPlan, ...] = ()` (alias
`plans`), validated by a `mode="before"` step so that:

- when `plans`/`keyword_plans` is given, `keywords` is derived from it (the plan
  list is the source of truth);
- when it is not given, plans are derived from `keywords` using the run-level
  `scroll_depth` and `grid` — the library/API path and every existing test keep
  working unchanged.

Two helpers keep the scraper free of index arithmetic:
`depth_of(keyword_index) -> int` and `grid_of(keyword_index) -> int` (1-based,
matching the `keyword_index` the tiles already carry). `engine_settings()` keeps
its current rule, but "a grid is set" becomes "any plan has `grid > 1`". The
`keywords` field on `ScrapeSettings` stays what the engine reads, so no engine
code changes.

`run_id()` hashes the whole query dump, so giving a keyword a different depth or
grid starts a new resume queue — the same rule as changing `--depth` today.

### D4. Scraper changes (`scraper.py`)

- `_enqueue_run`: geocode once. Request a bounding box when any plan has
  `grid > 1`; a run whose keywords are all whole-city does not need one (today's
  behaviour). The "could not resolve a bounding box" result still ends the run
  before anything is queued.
- `_queue_keyword`: takes that keyword's grid. `grid > 1` builds cells from the
  shared box; otherwise one whole-city tile. Each tile's settings already carry
  their own depth, so per-keyword depth needs no queue change.
- `_TileRun._start_tile`: print the keyword's depth (`self._query.depth_of(...)`)
  and its grid in the `▶ Grid NxN` / `▶ Creating scrape` lines.
- `_TileRun._summarize`: gate the per-keyword totals on that keyword's grid, not
  the run's.
- `_TileRun._split`, the `_sub` handling, the saturated check in `_handle`, and
  `SATURATED` are deleted; `_handle`'s success path ends after `_persist`.

### D5. Deep scan removal, complete list

- `cli/google_map_scrape_cli.py`: `--deep-scan`, its help, its plan line, its
  log field, and the pre-run deep-scan estimate block.
- `place_query.py`: the `deep_scan` field and its `grid = 3` defaulting branch.
- `scraper.py`: `SATURATED`, `_split`, the `_sub` tag in `_start_tile`, the split
  branch in `_handle`, the deep-scan print in `_start_tile`.
- `google_map/geo.py`: `GridCell._sub`.
- `place_queue.py`: `insert_after_keyword` (its only caller was the split).
- Tests: the three deep-scan tests in
  `tests/services/google_map_scraper/test_scraper_queue.py`.
- Docs: the `--deep-scan` row and the deep-scan mentions in
  `.opencode/skills/google-map-scrape-cli/SKILL.md`.
- Historical specs and plans under `docs/superpowers/` are left untouched; they
  record what was true when they were written.

### D6. Dry run and output

`print_plan` shows the per-keyword configuration, one line per keyword, then the
totals:

```
Dry run: nothing will be scraped or written.

Search: in Austin, TX, US
  "coffee shops"  depth 6  tiles 1
  "pizza"         depth 3  tiles 4 (2x2)
Scrapes: 5
Tiles: 5 (4 from grids)
Proxies: 0
Row limit: none
Filters: none
```

The rate-limit heads-up triggers when any keyword has `depth >= 10` or
`grid >= 2`, instead of the run-level check.

## Tests

- `tests/cli/test_google_map_scrape_cli.py`: repeated `-k ... --depth ... --grid`
  pairs produce the expected plans; omitted values give depth 5 and grid 1;
  `--keywords` is rejected; `--depth` before any keyword exits with the named
  error; no keyword exits with the required error; the proxies and dry-run tests
  keep passing (their meaning is unchanged).
- `tests/services/google_map_scraper/`: a two-keyword run with grids 1 and 2
  searches 1 and 4 tiles respectively, each printed with its own depth; every
  existing `_query(grid=2)` test still passes through the derived-plans path.
- The three deep-scan tests are deleted, not skipped.

## What is left to the builder

- The exact wording and alignment of the dry-run keyword lines.
- Whether the argparse actions live at module level or as closures in
  `build_parser`; either is fine as long as `read_query` stays a thin
  validate-and-report step.
- Whether `KeywordPlan` lives in `place_query.py` (suggested) or a new module.
- Test file placement for the new scraper cases (existing queue test file
  suggested).
