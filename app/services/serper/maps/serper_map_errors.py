"""The errors a Serper call can raise, so callers act on the key, not the message."""

from app.utils.errors import ScraperError


class SerperError(ScraperError):
    """Base of every Serper failure the project raises on purpose."""


class SerperConfigError(SerperError, ValueError):
    """The scrape config or the key list cannot be used."""


class SerperGeocodeError(SerperError):
    """The city could not be resolved to coordinates."""


class SerperNoCreditsError(SerperError):
    """The key has no credits left; another key may still work."""


class SerperAuthError(SerperError):
    """The key is rejected; retrying it is pointless."""


class SerperRateLimitedError(SerperError):
    """The key was asked too often, too fast."""

    def __init__(self, message: str, retry_after: float | None = None):
        super().__init__(message)
        self.retry_after = retry_after


class SerperResponseError(SerperError):
    """Serper refused the request itself, so repeating it will not help."""

    def __init__(self, status: int, message: str):
        super().__init__(f"HTTP {status}: {message}")
        self.status = status


class SerperUnavailableError(SerperError):
    """Serper or the network failed for now; the same call may succeed later."""
