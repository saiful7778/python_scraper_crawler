"""The Browser process a scrape run uses"""

from .browser import Browser
from .browser_error import BrowserError
from .shared_browser import SharedBrowser
from .tab import ProxyTab


class BrowserSession:
    """One Browser process for a scrape run; it can be started again after a crash.

    With ``shared``, the session attaches to a :class:`SharedBrowser` instead of
    owning a process: kill and close leave the shared browser alone.
    """

    def __init__(
        self, locale: str, headless: bool, shared: SharedBrowser | None = None
    ):
        """Remember the browser settings and shared browser; nothing is started yet."""
        self._locale = locale
        self._headless = headless
        self._shared = shared
        self._browser: Browser | None = None

    @property
    def browser(self) -> Browser | None:
        """The running browser, or ``None`` when none is started."""
        if self._shared is not None:
            return self._shared.current()
        return self._browser

    @property
    def is_dead(self) -> bool:
        """True when there is no browser or its connection has closed."""
        browser = self.browser
        return bool(
            browser is None
            or browser.cdp is None
            or browser.cdp.closed
        )

    def start(self) -> None:
        """Launch Browser. Raises ``BrowserError`` when it cannot start."""
        if self._shared is not None:
            self._browser = self._shared.ensure()
            return
        self._browser = Browser(lang=self._locale, headless=self._headless)

    def restart(self) -> None:
        """Close the current browser, if any, and launch a new one."""
        if self._shared is not None:
            self._browser = self._shared.restart_if_dead(seen=self._browser)
            return
        self.close()
        self.start()

    def open_tab(self, proxy: str | None) -> ProxyTab:
        """Return a :class:`ProxyTab` for ``proxy`` in this browser."""
        if self._browser is None:
            raise BrowserError("cannot open a tab: the browser is not started")
        return ProxyTab(self._browser, proxy)

    def kill(self) -> None:
        """Kill the Browser process at once, for a watchdog or shutdown."""
        if self._shared is not None:
            return
        if self._browser:
            self._browser.kill()

    def close(self) -> None:
        """Close Browser gracefully and forget it; calling it again is harmless."""
        if self._shared is not None:
            return
        if self._browser:
            try:
                self._browser.close()
            finally:
                self._browser = None
