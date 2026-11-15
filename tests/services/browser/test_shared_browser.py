import pytest

from app.services.browser import shared_browser
from app.services.browser.browser_session import BrowserSession
from app.services.browser.shared_browser import SharedBrowser


class FakeCDP:
    def __init__(self):
        self.closed = False


@pytest.fixture
def fake_browser_cls(monkeypatch: pytest.MonkeyPatch):
    class FakeBrowser:
        created = 0
        last = None

        def __init__(self, lang, headless):
            type(self).created += 1
            type(self).last = self
            self.cdp = FakeCDP()

        @property
        def closed(self) -> bool:
            return self.cdp.closed

        def close(self) -> None:
            self.cdp.closed = True

        def kill(self) -> None:
            self.cdp.closed = True

    monkeypatch.setattr(shared_browser, "Browser", FakeBrowser)
    return FakeBrowser


def test_shared_browser_starts_one_browser_for_two_sessions(fake_browser_cls):
    shared = SharedBrowser("en", True)
    a, b = BrowserSession("en", True, shared=shared), BrowserSession("en", True, shared=shared)
    a.start(); b.start()
    assert fake_browser_cls.created == 1
    assert a.browser is b.browser


def test_closing_one_attached_session_keeps_the_browser_for_the_other(fake_browser_cls):
    shared = SharedBrowser("en", True)
    a, b = BrowserSession("en", True, shared=shared), BrowserSession("en", True, shared=shared)
    a.start(); b.start()
    a.close(); a.kill()
    assert b.browser is not None and not b.browser.closed


def test_restart_after_a_crash_starts_one_new_browser(fake_browser_cls):
    shared = SharedBrowser("en", True)
    a, b = BrowserSession("en", True, shared=shared), BrowserSession("en", True, shared=shared)
    a.start(); b.start()
    fake_browser_cls.last.cdp.closed = True
    a.restart(); b.restart()
    assert fake_browser_cls.created == 2
