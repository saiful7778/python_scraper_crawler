"""The single exception type for CDP failures."""


class CDPError(Exception):
    """Raised when a CDP call fails, times out, or the connection is lost."""
