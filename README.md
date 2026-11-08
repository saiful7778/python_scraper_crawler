# python-scraper-crawler

> **⚠️ Caution: Educational purpose only.** This project is intended solely for learning and experimentation. Do not use it to scrape websites in violation of their terms of service, robots.txt rules, or applicable laws, and do not use collected data for spam, harassment, or any other unlawful purpose. Use it at your own risk.

Install Python dependencies with `uv sync`, then run `uv run python artisan serve`.
Check the application at http://127.0.0.1:8000 and its health endpoint at /health.
Run tests with `uv run pytest`.

If a database was selected, configure `.env` and run `uv run python artisan db:migrate`.
For a frontend, install Node.js 22.12 or newer and run `npm install` followed by
`npm run dev` in a separate terminal. Build production assets with `npm run build`.
Start the frontend dev server or build assets before opening a frontend page.
