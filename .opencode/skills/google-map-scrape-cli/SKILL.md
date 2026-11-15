---
name: google-map-scrape-cli
description: Use when the user wants to run this repo's Google Maps scraper CLI (cli/google_map_scrape_cli.py or the google-map-scrape command) to collect business listings for one or more keywords and a city into PostgreSQL, optionally through proxies, as a city grid or deep scan, keeping only rows with a phone number or website.
---

# Google Map Scrape CLI

Runs `cli/google_map_scrape_cli.py` from the repo root (installed as `google-map-scrape`). It scrapes Google Maps listings and saves them to PostgreSQL, skipping leads already stored. It does not write a CSV or JSON file, so there is no output path to report.

Each keyword is its own scrape. With `--grid`, each map tile is its own scrape. With `--proxies`, one tile runs per proxy at a time, so more proxies mean more tiles in parallel.

## Workflow

1. Gather `--keywords`, `--city`, and `--country` (2 letters). `--keywords` takes one or more business types separated by commas. Ask for anything else only if the user needs it.
2. Always run `--dry-run` first with the same flags and show the plan.
3. Confirm with the user before a real run. The CLI prints a heads-up for `--depth 10` or `--grid` 2 or more, because Google can rate-limit the IP. `--deep-scan` takes longer: the CLI prints its own estimate (about 25 s per tile; 3×3 by default gives roughly 3 to 18 min per city).
4. Run the real scrape with the same flags minus `--dry-run`, as a background job (see below). Never run a real scrape as a foreground shell call: the call can be aborted mid-run, which kills the scrape.
5. Check the log file until the run finishes, then report the lead count and any `⚠` or `✗` lines. There is no output file to check.

```bash
cd /home/saiful/programming/projects/python_scraper_crawler
uv run python cli/google_map_scrape_cli.py --keywords "coffee shops,bakeries" --city Austin --state TX --country us --dry-run
uv run python cli/google_map_scrape_cli.py --keywords "coffee shops,bakeries" --city Austin --state TX --country us --has-website --limit 100
```

`bootstrap.application` loads `.env` on import, so the console-script form `uv run --env-file .env google-map-scrape ...` works the same way.

## Running in the background

Run every real scrape as a background job, with output sent to a log file in `/tmp/opencode`:

```bash
cd /home/saiful/programming/projects/python_scraper_crawler
mkdir -p /tmp/opencode
nohup uv run python cli/google_map_scrape_cli.py --keywords "plumbers" --city Sydney --state NSW --country au --grid 3 > /tmp/opencode/plumbers_sydney.log 2>&1 &
echo $!
```

Use a log name that matches the run. Save the printed PID.

- Check progress: `tail -n 50 /tmp/opencode/plumbers_sydney.log`
- Check whether it is still running: `pgrep -af google_map_scrape_cli` (or `pgrep -af google-map-scrape`)
- Stop it and keep what was found so far (same as Ctrl-C): `kill -INT <pid>`. No new tiles start, in-flight tiles finish, rows buffered in Redis are saved to PostgreSQL on shutdown, and the tile queue is kept for the next run. Do not use `kill -9`.
- Rerunning the same command resumes the unfinished place run for that exact query; finished tiles are not searched again.
- Detailed application logs also go to `storage/logs/<date>.log`. Timestamps there are UTC.

## Flags

| Flag | Notes |
|------|-------|
| `--keywords`, `--city`, `--country` | Required. `--keywords` is a comma-separated list, e.g. `"coffee shops,bakeries"`; each keyword is its own scrape. A keyword cannot contain a comma. `--country` must be exactly 2 letters, such as `us`, `au`, `de`, and is lowercased. |
| `--state` | Optional region, e.g. `TX`. |
| `--lang XX` | Engine language, 2 letters, default `en`. Use the local language (`pt`, `es`, `de`) for non-English markets. |
| `--depth N` | Batches of results per keyword, 1 to 10, default 5. |
| `--max-time SECONDS` | Time limit per scrape, 1 to 1200, default 600. Each keyword, and each grid tile, is its own scrape. |
| `--grid N` | Split the city into an N×N grid, one scrape per tile, deduped; 1 to 10. Gets past Google's ~120-result cap per search. |
| `--deep-scan` | Runs a 3×3 grid unless `--grid` is set, then splits every tile that hits Google's cap (about 20 rows) into 4 sub-tiles. Up to 5× the tiles; slow. Use only for whole-city runs. |
| `--proxies URL,...` | Comma-separated; repeat the flag to add more (duplicates are dropped). Up to 100. Each entry is `http://[user:pass@]host:port`, `https://`, `socks5://`, or `socks5h://`. SOCKS5 does not accept user:pass. One tile runs per proxy at a time. |
| `--limit N` | Stop each scrape at N rows. Each keyword, and each grid tile, has its own limit. |
| `--has-phone`, `--has-website` | Keep only rows that have that field. |
| `--dry-run` | Print the plan and exit. Scrapes nothing and writes nothing. |

There are no `--website-depth`, `--format`, `--fields`, or `--output` flags. Website address resolution is always on (the plan prints `Website: level 1`); leads go to the database only.

Out-of-range numbers are rejected by validation even though argparse accepts them: `--depth` must be 1–10, `--max-time` 1–1200, and `--grid` 1–10.

## Output and behavior

- Before scraping, the CLI loads the lead keys already stored and skips duplicates. If that read fails, it exits before scraping with `✗ Could not read the existing leads from the database: ...`.
- It prints `▶ N known lead keys loaded; duplicates are skipped`, then progress per keyword or tile.
- Leads are saved to PostgreSQL as they finish. The final log line is `scrape done: N finished, M skipped, blocked=..., K leads`. No CSV or JSON file is written.
- `⚠ Google rate-limited this IP`: rows found before the block are kept. With proxies, a rate-limited proxy retires and its tile is retried on another proxy; a tile with no proxy left is skipped.
- `⚠ N scrape(s) skipped`: see the `⚠` lines above for the reason.
- `✗ No scrape finished`: the reason is in the `✗` line above it.
- If the database save fails and rows stay in Redis, the CLI exits with `✗ UnsavedLeadsError: ...` (or `ScrapeDatabaseError`). The Redis data and tile queue are kept, so the next run resumes; leads already saved stay in PostgreSQL.
- Ctrl-C stops the run from taking new tiles, lets in-flight tiles finish, saves buffered rows to PostgreSQL on shutdown, and keeps the queue. The CLI prints `⏸ Finishing the current cell, press Ctrl-C again to quit` and then `✗ Stopped.` A second Ctrl-C quits at once.
- Grid and deep-scan runs print per-cell lines (`cell i/N: ... rows, ... kept so far`) and a per-keyword summary.
- A rerun of the same command resumes the unfinished place run and prints `▶ Resuming the unfinished run of this command`; finished tiles are not searched again. The queue lives in Redis for 7 days.

## Common mistakes

- Skipping `--dry-run` and starting a large run without confirming.
- Expecting a CSV or JSON file, or passing `--format`, `--fields`, `--output`, or `--website-depth`; those flags do not exist.
- Using `--grid` or `--deep-scan` for a small city where `--limit` is enough.
- Passing a 3-letter or full country name to `--country`.
- Passing `--depth` outside 1–10, `--max-time` outside 1–1200, or `--grid` outside 1–10.
- Passing credentials in SOCKS5 proxy URLs. Use `http://user:pass@host:port` for auth.
- Assuming Ctrl-C leaves the leads unsaved. Buffered rows are saved on shutdown, but the run stays unfinished and the queue is kept.
- Assuming a resumed run re-searches tiles that already finished.
- Running a real scrape as a foreground shell call. The call can be aborted with "Tool execution interrupted", which stops the scrape and can leave the run unfinished.
- Using `kill -9` to stop a run. It skips the normal shutdown. Use `kill -INT` to match Ctrl-C.
