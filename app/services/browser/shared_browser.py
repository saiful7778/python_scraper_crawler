"""One Browser process that every scrape worker attaches to."""

import threading

from app.utils.logger import get_logger

from .browser import Browser

logger = get_logger(__name__)


def browser_is_dead(browser: Browser | None) -> bool:
    """True when there is no browser or its connection has closed."""
    return bool(browser is None or browser.cdp is None or browser.cdp.closed)


class SharedBrowser:
    """Owns one Browser; sessions attach to it instead of starting their own."""

    def __init__(self, locale: str, headless: bool):
        """Remember the browser language and headless mode; nothing is started yet."""
        self._locale = locale
        self._headless = headless
        self._lock = threading.Lock()
        self._browser: Browser | None = None

    def current(self) -> Browser | None:
        """The shared browser, or ``None`` when none is running."""
        return self._browser

    def ensure(self) -> Browser:
        """Start the shared browser if none is running and return it."""
        with self._lock:
            if browser_is_dead(self._browser):
                self._replace(self._browser)
            assert self._browser is not None
            return self._browser

    def restart_if_dead(self, seen: Browser | None) -> Browser:
        """Replace the browser the caller saw die, unless a sibling already did."""
        with self._lock:
            current = self._browser
            if (
                current is not None
                and current is not seen
                and not browser_is_dead(current)
            ):
                return current
            self._replace(current)
            assert self._browser is not None
            return self._browser

    def close(self) -> None:
        """Close the shared browser and forget it; calling it again is harmless."""
        with self._lock:
            if self._browser:
                try:
                    self._browser.close()
                finally:
                    self._browser = None

    def _replace(self, old: Browser | None) -> None:
        """Kill ``old`` so its process is not orphaned, then launch a new browser."""
        self._browser = None
        if old is not None:
            try:
                old.kill()
            except Exception:
                logger.warning("could not kill the dead shared browser", exc_info=True)
        self._browser = self._launch()

    def _launch(self) -> Browser:
        return Browser(lang=self._locale, headless=self._headless)
