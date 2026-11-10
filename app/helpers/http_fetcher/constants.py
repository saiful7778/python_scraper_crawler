"""Tunables and fixed lookup tables for the plain-HTTP fetcher."""

SITE_TIMEOUT = 10  # seconds per website request (each socket read)
SITE_HARD = 30  # seconds a whole fetch may take, then its socket is closed
UA = "Mozilla/5.0 (compatible; python_scraper_crawler-scraper/3.0)"
RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
BODYLESS_STATUSES = frozenset({204, 205})
DEFAULT_CONTENT_TYPES = (
    "text/html",
    "application/xhtml+xml",
    "text/plain",
    "application/xml",
    "text/xml",
)
READ_CHUNK_BYTES = 1 << 14
