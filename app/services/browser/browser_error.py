"""The single exception type for browser lifecycle failures."""

from app.utils.errors import ScraperError


class BrowserError(ScraperError, RuntimeError):
    """Raised when Chrome cannot be located, downloaded, launched, or
    connected to. Distinct from ``CDPError``, which covers failures while
    talking to a Chrome that is already running."""
