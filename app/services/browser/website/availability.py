"""Check that website rendering can run before any scrape depends on it."""

from ..browser_error import BrowserError
from ..locator import locate_chrome_binary


class RenderUnavailableError(BrowserError):
    """Website rendering cannot run: Playwright or a Chrome binary is missing."""


def check_renderer_available() -> str:
    """Return the Chrome/Chromium path used for rendering.

    :raises RenderUnavailableError: If Playwright cannot be imported or no
        Chrome binary is found. The message says which one is missing.
    """
    try:
        import playwright.async_api  # noqa: F401
    except ImportError as error:
        raise RenderUnavailableError(
            "Website rendering needs the playwright package, which cannot be imported "
            f"({error}). Install it in your virtual environment with "
            "`pip install playwright`, then run `playwright install chromium`."
        ) from error

    path = locate_chrome_binary()
    if not path:
        raise RenderUnavailableError(
            "Website rendering needs Chrome/Chromium, and none was found. "
            "Install Chrome or set CHROME to its path."
        )
    return path
