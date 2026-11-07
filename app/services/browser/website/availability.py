"""Check that website rendering can run before any job depends on it."""

from ..locator import locate_chrome_binary


def check_renderer_available() -> str:
    """Return the Chrome/Chromium path used for rendering.

    :raises RuntimeError: If Playwright is not importable or no Chrome
        binary is found.
    """
    try:
        import playwright  # noqa: F401
    except (ImportError, TypeError):
        raise RuntimeError(
            "playwright is not installed. Run: playwright install inside your virtual environment."
        )

    path = locate_chrome_binary()
    if not path:
        raise RuntimeError(
            "Website rendering needs Chrome/Chromium. Install Chrome or set CHROME to its path."
        )
    return path
