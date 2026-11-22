# python-scraper-crawler

> **⚠️ Caution: Educational purpose only.** This project is intended solely for learning and experimentation. Do not use it to scrape websites in violation of their terms of service, robots.txt rules, or applicable laws, and do not use collected data for spam, harassment, or any other unlawful purpose. Use it at your own risk.

Install Python dependencies with `uv sync`, then run `uv run python artisan serve`.
Check the application at http://127.0.0.1:8000 and its health endpoint at /health.
Run tests with `uv run pytest`.

If a database was selected, configure `.env` and run `uv run python artisan db:migrate`.
For a frontend, install Node.js 22.12 or newer and run `npm install` followed by
`npm run dev` in a separate terminal. Build production assets with `npm run build`.
Start the frontend dev server or build assets before opening a frontend page.

## Scraping Google Maps through Serper

The browser scraper is slow and gets rate-limited. The Serper command does the
same job through Serper's `/maps` endpoint instead: one API call per page, 3
credits per call, at most 20 places per page.

```bash
# one keyword: grid 3 splits the city 3x3, depth 2 reads two pages per tile
uv run serper-map-scrape \
  --keyword plumbers --depth 2 --grid 3 \
  --city Austin --state TX --country us

# several keywords, each with its own depth and grid
uv run serper-map-scrape \
  --keyword plumber --depth 2 --grid 4 \
  --keyword hvac --depth 4 --grid 2 \
  --city Austin --state TX --country us
```

`SERPER_API_KEYS` in `.env` holds a comma-separated list of keys. Every key runs
`--parallel` requests at a time (default 5) and rests 1.5 s between batches; when
one key runs out of credits its work moves to the others.

`--depth` and `--grid` belong to the `--keyword` flag in front of them, so each
keyword gets its own effort: `--keyword plumber --grid 4` searches 4x4 tiles while
`--keyword hvac --grid 2` searches 2x2. Both default to 1, the whole city and one
page; a `--depth` or `--grid` before any `--keyword` is an error. The first
command above is 3 x 3 x 2 = 18 calls, about 54 credits; the second is
4 x 4 x 2 + 2 x 2 x 4 = 48 calls, about 144 credits. Run with `--dry-run` to see
the plan and the cost per keyword before spending anything.

Jobs and their requests are stored in `serper_map_jobs` and
`serper_map_requests`. If a run stops because the keys ran out of credits, the
same command offers to continue that job; already-fetched pages are not fetched
again, and places already in `leads` are skipped. `--dry-run` prints the plan
and the cost without writing anything, and `--has-phone` / `--has-website` keep
only rows with that field (the website filter also checks whether the site can
be crawled).

Apply the tables with the Tortoise migration command:

```bash
uv run --env-file .env tortoise -c config.tortoise.TORTOISE_ORM migrate
```
