"""The Browser process a job run uses"""

from .browser import Browser
from .tab import ProxyTab


class BrowserSession:
    """One Browser process for a job run; it can be started again after a crash."""

    def __init__(self, locale: str, headless: bool):
        """Remember the browser language and headless mode; nothing is started yet."""
        self._locale = locale
        self._headless = headless
        self._browser: Browser | None = None

    @property
    def browser(self) -> Browser | None:
        """The running browser, or ``None`` when none is started."""
        return self._browser

    @property
    def is_dead(self) -> bool:
        """True when there is no browser or its connection has closed."""
        return bool(
            self._browser is None
            or self._browser.cdp is None
            or self._browser.cdp.closed
        )

    def start(self) -> None:
        """Launch Browser. Raises ``BrowserError`` when it cannot start."""
        self._browser = Browser(lang=self._locale, headless=self._headless)

    def restart(self) -> None:
        """Close the current browser, if any, and launch a new one."""
        self.close()
        self.start()

    def open_tab(self, proxy: str | None) -> ProxyTab:
        """Return a :class:`ProxyTab` for ``proxy`` in this browser."""
        assert self._browser is not None
        return ProxyTab(self._browser, proxy)

    def kill(self) -> None:
        """Kill the Browser process at once, for a watchdog or shutdown."""
        if self._browser:
            self._browser.kill()

    def close(self) -> None:
        """Close Browser gracefully and forget it; calling it again is harmless."""
        if self._browser:
            self._browser.close()
            self._browser = None
