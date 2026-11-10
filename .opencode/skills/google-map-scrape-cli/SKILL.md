---
name: google-map-scrape-cli
description: Use when the user wants to run this repo's Google Maps scraper CLI (cli/google_map_scrape_cli.py or the google-map-scrape command) to collect business listings for a keyword and city, export them to CSV or JSON, or apply phone, email, or website filters to the results.
---

# Google Map Scrape CLI

Runs `cli/google_map_scrape_cli.py` from the repo root. It scrapes Google Maps listings, saves them to the database (skipping leads already stored), and writes them to a CSV or JSON file.

## Workflow

1. Gather `--keyword`, `--city`, and `--country` (2 letters). Ask for anything else only if the user needs it.
2. Always run `--dry-run` first with the same flags and show the plan.
3. Confirm with the user before a real run. The CLI prints a heads-up for `--depth 10` or `--grid` 2 or more, because Google can rate-limit the IP. `--deep-scan` takes longer: the CLI prints its own estimate (about 25 s per tile; 3×3 by default gives roughly 3 to 18 min per city).
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
| `--keyword`, `--city`, `--country` | Required. `--country` must be exactly 2 letters, such as `us`, `de`, `br`. |
| `--state` | Optional region, e.g. `TX`. Not part of the default file name. |
| `--lang XX` | Engine language, 2 letters, default `en`. Use the local language (`pt`, `es`, `de`) for non-English markets. |
| `--depth N` | Batches of results per keyword, 1 to 10, default 5. |
| `--max-time SECONDS` | Time limit per scrape, 1 to 1200, default 600. |
| `--grid N` | Split the city into N×N tiles, max 10, one scrape each, deduped. Gets past Google's ~120-result cap per search. |
| `--deep-scan` | Runs the grid (3×3 unless `--grid` is set), then splits every tile that hits Google's cap (about 20 rows) into 4 smaller tiles. Up to 5× the tiles. Slow. Use only for whole-city runs. |
| `--website-depth 1` | The only accepted value. Crawls the website for emails, socials, and tech. Any other value is rejected. |
| `--proxy URL` | Repeatable. `http://[user:pass@]host:port`, `https://`, `socks5://`, or `socks5h://`. SOCKS5 does not accept user:pass. Scrapes take turns through the proxies. |
| `--has-phone`, `--has-email`, `--has-website` | Keep only rows that have that field. `--has-email` means an email was found on the website. |
| `--limit N` | Stop at N rows. |
| `--format csv\|json` | Default `csv`. CSV is UTF-8 with a BOM, so Excel opens it cleanly. |
| `--fields a,b,c` | Columns to write, in that order, default all. A name the scrape does not produce prints a `⚠` line and is written empty. |
| `--output PATH` | Default `./storage/scrape-leads/leads_<keyword>_<city>.<format>`, e.g. `leads_coffee_shops_austin.csv`. The default folder is created automatically. A custom `--output` folder must already exist and be writable, or the CLI exits before scraping. |
| `--dry-run` | Prints the plan and exits. Scrapes nothing, writes nothing, creates no folder. |

## Output and behavior

- Before scraping, the CLI loads the lead keys already in the database and skips duplicates. If that read fails, it exits before scraping.
- After scraping, leads are saved to the database, then written to the output file.
- If the database save fails entirely, the CLI exits **without writing the file**. Individual rows that fail print `⚠ N lead(s) could not be saved` and the file is still written.
- Ctrl-C writes the leads found so far to the output file, but does **not** save them to the database.
- `⚠ Google rate-limited this IP`: rows found before the block are kept. Add `--proxy` and retry later.
- `⚠ N scrape(s) skipped`: see the `⚠` lines above for the reason.
- `✗ No scrape finished`: the reason is in the `✗` line above it.

## Common mistakes

- Skipping `--dry-run` and starting a large run without confirming.
- Using `--grid` or `--deep-scan` for a small city where `--limit` is enough.
- Passing a 3-letter or full country name to `--country`.
- Passing `--website-depth 2` or `3`. Only `1` is accepted.
- Writing `--output` into a folder that does not exist.
- Passing credentials in SOCKS5 proxy URLs. Use `http://user:pass@host:port` for auth.
- Assuming Ctrl-C saved the leads to the database. It only saved the file.
- Forgetting that a plain shell call times out after 2 minutes.
