"""The single exception type for CDP failures."""

from app.utils.errors import ScraperError


class CDPError(ScraperError):
    """Raised when a CDP call fails, times out, or the connection is lost."""
