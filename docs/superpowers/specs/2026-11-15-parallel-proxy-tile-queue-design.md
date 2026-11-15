# Parallel proxy tile queue: design

## Intent

Today a city run searches one tile (grid cell) at a time, and the keywords run
one after another. That is slow for deep scans: 5 keywords with `--deep-scan`
and a 3 x 3 grid can mean up to 225 tiles, each about 25 s, run one by one.

Google rate-limits per IP. With several proxies, each proxy can search a tile
without slowing the others. This change runs one tile per proxy in parallel,
and takes tiles from a single shared queue so that the proxies stay busy.

It builds on `2026-11-15-place-scrape-queue-design.md`, which put keyword jobs
in a Redis queue. This design moves the queue unit from keyword to tile.

## Goals

- `--proxy URL` (repeatable) becomes `--proxies URL,URL,...`, a comma-separated
  list. The flag stays optional.
- The run has a single Redis queue of **tiles** across all keywords, in FIFO
  order. A keyword without `--grid` is one tile.
- Deep-scan sub-tiles are added to the same queue when a tile reaches Google's
  cap, as today.
- With N proxies there are N workers. Each worker is bound to one proxy and runs
  one tile at a time. When a worker finishes a tile, it takes the next tile from
  the queue.
- With no proxies there is one worker with no proxy. This matches the current
  sequential behaviour.
- All workers share one Chrome process per run. Each worker has its own
  browser context (`Target.createBrowserContext`) with its proxy set.
- A rate-limited proxy stops only its own worker. The run continues on the other
  workers.
- Resume works per tile: a tile that was in flight when the process stopped runs
  again. Finished tiles are never searched again.

## Amendment: one session, a worker pool

Found while planning: `run_scrape` builds a `ScrapeSession` per call. Its start
calls `requeue_interrupted()` (PostgreSQL) and `requeue_processing()` (Redis),
and `ScrapeWorker` claims the oldest pending scrape with `claim_next_pending()`.
All three act on every scrape, so several sessions at once would reset and steal
each other's tiles. Parallel tiles therefore run inside **one** session:

- `PlaceScraper` starts one `ScrapeSession` for the whole city run. The session
  starts the Flusher and the database connection once. The session's resume step
  runs once, at the start of the run.
- The single `ScrapeWorker` thread becomes a pool of N worker threads, one per
  proxy (1 with no proxies). Each thread claims a specific tile scrape by its
  ID, with `claim_by_id`. A thread never claims another thread's tile.
- A `ScrapeRun` takes a browser context from the caller, created from the one
  shared `Browser`. It does not start or close its own `BrowserSession`.
- Dedupe, kept rows and the queue are still owned by the coordinator, as above.

## Non-goals

- One Chrome process per worker.
- Changes to the PostgreSQL schema, the web UI, or the CSV output.
- Changes to the filters, the `--limit` meaning, or the deep-scan split rule.
  `--limit` still applies to each tile, as the help text says.
- Parallel runs of different city commands sharing one queue. Each run has its
  own queue.

## Anti-goals

- Two tiles must never run at the same time on the same proxy. A tile's retries
  never move to another worker's proxy.
- A finished tile's leads must never be lost, and a finished tile must never run
  again after a crash or resume.
- With no `--proxies`, the run must behave exactly as it does today: one tile at
  a time, same output, same messages in the common path.
- One proxy failing must not stop the other proxies' tiles.

## Behaviour

**CLI.** `--proxies` takes a comma-separated list. Each entry is validated with
`parse_proxy_urls` in `app/utils/proxies.py`. Blank entries and repeats are
dropped, as `comma_list` does for keywords. `--proxy` is replaced by
`--proxies`. The `FLAG_NAMES` mapping in the CLI and the dry-run plan are updated
to match.

**Tile identity.** A tile is one search: a keyword, its settings (with the cell
centre, radius and zoom), and its bounding box. Tile jobs are stored as JSON.
Tiles of all keywords go into one queue in keyword order, then grid order.

**Queue.** The queue lives in Redis under the run's `place_queue:<run id>:`
namespace. It holds three things:

- the pending tiles, in FIFO order,
- the in-flight tiles, each with the worker that took it,
- the finished tiles, with their kept rows and counts.

A worker takes a tile with a single atomic Redis step, so a tile is never handed
to two workers. The run id is the hash of the full `PlaceQuery`, as in the
existing queue design.

**Workers.** The coordinator starts N worker threads, where N is the number of
proxies (1 with no proxies). Each worker:

1. takes the next pending tile, if any, and marks it in flight,
2. runs the tile through the shared browser, in its own context, with its proxy,
3. reports the result to the coordinator, and loops.

A worker with no tile left exits.

**Round-robin.** Each worker keeps its proxy for the whole run, so the proxies
are used in turn: when tiles take about the same time, the proxies take turns in
order. *(My call: I did not add a separate rotation table. Taking the next tile
when free gives round-robin in practice. The existing `ProxyRotation` in
`app/utils/proxies.py` is not needed for this.)*

**Browser.** One `Browser` is started for the run and is shared by all workers.
Each worker creates one browser context with `Browser.context(proxy)` when it
starts, and reuses it for all its tiles. The context is disposed when the worker
exits. The browser is closed when the run ends. With no proxy, the single worker
uses a context without a proxy.

**Rate limit.** When a tile reports `blocked`:

- its worker stops taking tiles, and its context is disposed,
- the tile goes back to the front of the pending queue, so a healthy proxy runs
  it,
- the other workers continue.

A tile has an attempt count. It can be re-queued at most once per proxy. After
that it is counted as skipped. So if every proxy blocks a tile, the tile does not
bounce between workers, and the run ends when all workers have stopped. *(My
call: the attempt cap is one per proxy.)*

**Proxy failure.** A tile that fails with a proxy error is retried only on the
worker's own proxy, inside `ScrapeRun` and `KeywordSearch`. The retry rotation is
limited to that one proxy, so a tile never runs on another worker's proxy. This
keeps the rule that one proxy runs one tile at a time. If the own-proxy retries
are exhausted, the tile is re-queued and counts against its attempt cap. The
failing worker then stops taking tiles, as with a block. The other workers
continue. The failed mark lasts for the whole run. *(My call: a failing proxy is
treated like a blocked one.)*

**Lost Chrome connection.** The browser is shared, so a lost connection stops
every worker. The run stops, and the queue is kept, including the in-flight
tiles, so the next run resumes them. *(My call: this matches the crash rule
below.)*

**Dedupe and limits.** Each finished tile's rows are deduped against the rows
the run has kept so far and against `known_keys`. This is done by the
coordinator, one tile at a time, as the rows arrive. The `--limit` applies to
each tile's rows, as today.

**Deep scan.** When a tile finishes with at least `SATURATED` raw rows (rows
returned by Google for that tile, before dedupe), and deep scan is on, the
coordinator adds its four sub-tiles to the end of the pending queue. Sub-tiles
never split again. *(My call: raw rows, as the code does today.)*
Placement by path: with no `--proxies` the sub-tiles go after the same keyword's
pending cells and before the next keyword, as the base grid runs them. This
departs from "end of the pending queue" on the no-proxy path only; with proxies
the sub-tiles go to the end of the pending queue, as written above.

**Progress and resume.** When a tile finishes, its kept rows and counts go to
the done list, and the tile leaves the in-flight set, in one Redis transaction.
On resume, every in-flight tile is put back at the front of the pending queue
and runs again. A tile is in flight from the moment a worker takes it until it
finishes.

**Ctrl-C.** The first Ctrl-C stops the workers from taking new tiles. Tiles in
flight finish. The second Ctrl-C quits, as today. `salvage_rows` holds the kept
rows of all finished tiles.

**Clearing.** When the run ends normally or every worker stopped on a rate limit
or a proxy failure, the queue is deleted. After a lost Chrome connection, a crash
or an error, it is kept for the next run, so the in-flight tiles are resumed.
*(My call: a lost connection keeps the queue. The keyword-queue design deleted it,
but this design resumes per tile.)*

**Run identity and proxies.** `PlaceQuery` includes the proxy list, so the run id
changes when `--proxies` changes. A resume with a different list starts a new
queue. *(My call: the proxy list is part of the query.)*

**Output.** The summary keeps today's counts: finished, skipped, blocked, and
the number of kept leads. A skipped tile counts as skipped.

## Constraints

- Redis is the queue store. Its errors are raised as `BufferError`.
- Tile jobs are stored as JSON, so bounding boxes are lists when read back.
- The worker threads are the only writers of the browser. The coordinator is the
  only writer of the queue's done list and of the deduped rows.
- Google is searched by one worker per proxy at a time. Nothing else runs in
  parallel.

## Left to the builder

- Naming of the modules and methods, as long as they follow the code around them.
- How the existing `ScrapeRun` takes a browser context from the caller, instead of
  starting its own `BrowserSession`. Today `ScrapeRun` creates and kills its own
  browser (`scrape/scrape_run.py`, lines 84 and 98). This must change, so that
  `run_scrape` can use a shared browser and a per-worker context.
- Whether `run_scrape` and its `ScrapeSession` can run on several threads at
  once. Each call creates its own Redis buffers and threads, keyed by scrape id.
  The builder must check this before relying on it.
- How `ProxyAuthenticator` (in `app/services/browser/`) sets credentials when
  several contexts are open. Today it works per tab.

## Implementation notes (from the build)

- A blocked or proxy-failed tile keeps the rows its attempts found, capped at
  the tile's one `--limit`. A re-queued tile carries those rows in its queue
  record, so a resume keeps them too.
- With several proxies, a shared lead is attributed to the keyword whose tile
  finished first. Totals and the CSV are unaffected.
- A watchdog timeout on a multi-proxy tile cannot kill the shared browser, so a
  stuck CDP call there ends only at its own client timeout.
- A tile whose Chrome connection was lost reruns as a new scrape. Its rows were
  already saved with `ignore_conflicts`, so a resume may save the same leads
  twice.
- `--proxies` may be repeated: the values accumulate and repeats collapse.
