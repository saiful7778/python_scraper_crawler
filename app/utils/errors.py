"""The base of every error this project raises on purpose, and a helper to report any error.

A message names the step that failed and the reason. Errors are raised
``from`` the original exception, so the traceback keeps the exact line.
"""


class ScraperError(Exception):
    """Base class for every error the project raises deliberately."""


class ConfigError(ScraperError, ValueError):
    """An environment variable or setting has a value the program cannot use."""


class ProxyConfigError(ScraperError, ValueError):
    """A proxy URL is malformed. The message never shows the credentials."""


def describe(error: BaseException) -> str:
    """Return ``ExceptionType: message``, or just the type when the message is empty."""
    text = str(error)
    name = type(error).__name__
    return f"{name}: {text}" if text else name
