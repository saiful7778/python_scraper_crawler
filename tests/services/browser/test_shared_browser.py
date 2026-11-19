from typing import Protocol, cast

import pytest

from app.services.browser import shared_browser
from app.services.browser.browser_session import BrowserSession
from app.services.browser.shared_browser import SharedBrowser


class _FakeCDPLike(Protocol):
    closed: bool


class _FakeBrowserLike(Protocol):
    cdp: _FakeCDPLike
    closed: bool
    killed: bool


class FakeCDP:
    def __init__(self):
        self.closed = False


@pytest.fixture
def fake_browser_cls(monkeypatch: pytest.MonkeyPatch):
    class FakeBrowser:
        created = 0
        last = None
        kill_error: Exception | None = None

        def __init__(self, lang, headless):
            type(self).created += 1
            type(self).last = self
            self.cdp = FakeCDP()
            self.killed = False

        @property
        def closed(self) -> bool:
            return self.cdp.closed

        def close(self) -> None:
            self.cdp.closed = True

        def kill(self) -> None:
            self.killed = True
            error = type(self).kill_error
            if error is not None:
                raise error
            self.cdp.closed = True

    monkeypatch.setattr(shared_browser, "Browser", FakeBrowser)
    return FakeBrowser


def test_shared_browser_starts_one_browser_for_two_sessions(fake_browser_cls):
    shared = SharedBrowser("en", True)
    a = BrowserSession("en", True, shared=shared)
    b = BrowserSession("en", True, shared=shared)
    a.start()
    b.start()
    assert fake_browser_cls.created == 1
    assert a.browser is b.browser


def test_closing_one_attached_session_keeps_the_browser_for_the_other(fake_browser_cls):
    shared = SharedBrowser("en", True)
    a = BrowserSession("en", True, shared=shared)
    b = BrowserSession("en", True, shared=shared)
    a.start()
    b.start()
    a.close()
    a.kill()
    assert b.browser is not None
    assert not cast(_FakeBrowserLike, b.browser).closed


def test_restart_after_a_crash_starts_one_new_browser(fake_browser_cls):
    shared = SharedBrowser("en", True)
    a = BrowserSession("en", True, shared=shared)
    b = BrowserSession("en", True, shared=shared)
    a.start()
    b.start()
    fake_browser_cls.last.cdp.closed = True
    a.restart()
    b.restart()
    assert fake_browser_cls.created == 2


def test_restart_kills_the_old_browser_before_replacing_it(fake_browser_cls):
    shared = SharedBrowser("en", True)
    a = BrowserSession("en", True, shared=shared)
    a.start()
    old = a.browser
    assert old is not None
    old_view = cast(_FakeBrowserLike, old)
    old_view.cdp.closed = True
    a.restart()
    assert old_view.killed
    assert a.browser is not old
    assert fake_browser_cls.created == 2


def test_restart_still_replaces_the_browser_when_kill_fails(fake_browser_cls):
    fake_browser_cls.kill_error = OSError("already gone")
    shared = SharedBrowser("en", True)
    a = BrowserSession("en", True, shared=shared)
    a.start()
    old = a.browser
    assert old is not None
    old_view = cast(_FakeBrowserLike, old)
    old_view.cdp.closed = True
    a.restart()
    assert old_view.killed
    assert a.browser is not old
    assert fake_browser_cls.created == 2


def test_a_sibling_opens_its_tab_on_the_browser_another_session_restarted(
    fake_browser_cls,
):
    shared = SharedBrowser("en", True)
    a = BrowserSession("en", True, shared=shared)
    b = BrowserSession("en", True, shared=shared)
    a.start()
    b.start()
    dead = fake_browser_cls.last
    dead.cdp.closed = True
    a.restart()
    fresh = fake_browser_cls.last
    assert fresh is not dead
    assert b.is_dead is False
    assert b.open_tab(None)._browser is fresh
