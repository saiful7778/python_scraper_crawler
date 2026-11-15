"""One Browser process that every scrape worker attaches to."""

import threading

from .browser import Browser


def _is_dead(browser: Browser | None) -> bool:
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
            if _is_dead(self._browser):
                self._browser = Browser(lang=self._locale, headless=self._headless)
            return self._browser

    def restart_if_dead(self, seen: Browser | None) -> Browser:
        """Replace the browser the caller saw die, unless a sibling already did."""
        with self._lock:
            current = self._browser
            if current is not None and current is not seen and not _is_dead(current):
                return current
            self._browser = Browser(lang=self._locale, headless=self._headless)
            return self._browser

    def close(self) -> None:
        """Close the shared browser and forget it; calling it again is harmless."""
        with self._lock:
            if self._browser:
                try:
                    self._browser.close()
                finally:
                    self._browser = None
