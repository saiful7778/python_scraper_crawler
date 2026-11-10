---
name: google-map-scrape-cli
description: Use when the user wants to run this repo's Google Maps scraper CLI (cli/google_map_scrape_cli.py or the google-map-scrape command) to collect business listings for a keyword and city, export them to CSV or JSON, or apply phone, email, or website filters to the results.
---

# Google Map Scrape CLI

Runs `cli/google_map_scrape_cli.py` from the repo root. It scrapes Google Maps listings, writes them to a file, and saves them to the database.

## Workflow

1. Gather `--keyword`, `--city`, and `--country` (2 letters). Ask for anything else only if the user needs it.
2. Always run `--dry-run` first with the same flags and show the plan.
3. Confirm with the user before a real run. Large runs (`--depth` 10, or `--grid` of 2 or more) can get the IP rate-limited, and `--deep-scan` can take 20 to 100+ minutes per city.
4. Run the real scrape with the same flags minus `--dry-run`. Scrapes can take minutes, so use `timeout: 0` or `background: true` in the shell tool.
5. Report the output path, the lead count, and any `⚠` or `✗` lines.

```bash
cd /home/saiful/programming/projects/python_scraper_crawler
uv run python cli/google_map_scrape_cli.py --keyword "coffee shops" --city Austin --state TX --country us --dry-run
uv run python cli/google_map_scrape_cli.py --keyword "coffee shops" --city Austin --state TX --country us --has-website --limit 100
```

## Flags

| Flag | Notes |
|------|-------|
| `--keyword`, `--city`, `--country` | Required. `--country` is a 2-letter code such as `us`, `de`, `br`. |
| `--state` | Optional region, e.g. `TX`. |
| `--lang XX` | Engine language, default `en`. Use the local language (`pt`, `es`, `de`) for non-English markets. |
| `--depth N` | Batches of results per keyword, 1 to 10, default 5. |
| `--max-time SECONDS` | Time limit per scrape, 1 to 1200, default 600. |
| `--grid N` | Split the city into N×N tiles, max 10. Use this to get past Google's ~120-result cap. |
| `--deep-scan` | Runs the grid (3×3 unless `--grid` is set), then splits capped tiles further. Slow. Use only for whole-city runs. |
| `--website-depth 1\|2` | 1 = homepage emails and socials. 2 = also renders JavaScript sites. `3` falls back to 2. |
| `--proxy URL` | Repeatable. `http://`, `https://`, `socks5://`, or `socks5h://`. SOCKS5 does not accept user:pass. Scrapes rotate through proxies. |
| `--has-phone`, `--has-email`, `--has-website` | Keep only rows with that field. |
| `--limit N` | Stop at N rows. |
| `--format csv\|json` | Default `csv`. |
| `--fields a,b,c` | Columns to write, in order. Unknown names are written empty. |
| `--output PATH` | Default `/storage/scrape-leads/leads_<keyword>_<city>.<format>`. The folder must exist and be writable, or the CLI exits before scraping. |

## Output and behavior

- Rows are saved to the database and written to the output file. A database failure happens after the scrape, and the CSV is still written.
- Ctrl-C saves the leads found so far before exiting.
- `⚠ Google rate-limited this IP`: rows found before the block are kept. Add `--proxy` and retry later.
- `✗ No scrape finished`: check the `✗` line above it for the reason.

## Common mistakes

- Skipping `--dry-run` and starting a large run without confirming.
- Using `--grid` or `--deep-scan` for a small city where `--limit` is enough.
- Passing a 3-letter or full country name to `--country`.
- Writing `--output` into a folder that does not exist.
- Passing credentials in SOCKS5 proxy URLs. Use `http://user:pass@host:port` for auth.
- Forgetting that a plain shell call times out after 2 minutes.
