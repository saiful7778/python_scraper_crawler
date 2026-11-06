import os
import subprocess
from types import SimpleNamespace

import pytest

from app.services.browser import browser as browser_module
from app.services.browser.browser import Browser
from app.services.browser.browser_error import BrowserError
from app.services.browser.cdp_client import CDPError

CHROME = "/usr/bin/fake-chrome"
DEVTOOLS_PATH = "/devtools/browser/abc"


class FakeCDP:
    def __init__(self, url: str, timeout: float = 30.0) -> None:
        self.url = url
        self.timeout = timeout
        self.user_agent: str | None = None
        self.closed = False
        self.native_user_agent = "Mozilla/5.0 (X11) HeadlessChrome/120.0 Safari"
        self.calls: list[tuple[str, dict | None]] = []

    def call(self, method, params=None):
        self.calls.append((method, params))
        if method == "Browser.getVersion":
            return {"userAgent": self.native_user_agent, "product": "Chrome/120.0"}
        if method == "Target.createBrowserContext":
            return {"browserContextId": "ctx-1"}
        return {}

    def close(self) -> None:
        self.closed = True


class FakeProcess:
    def __init__(self, hang_once: bool = False) -> None:
        self.running = True
        self.hang_once = hang_once
        self.events: list[str] = []

    def poll(self):
        return None if self.running else 0

    def terminate(self) -> None:
        self.events.append("terminate")

    def kill(self) -> None:
        self.events.append("kill")

    def wait(self, timeout=None):
        self.events.append("wait")
        if self.hang_once:
            self.hang_once = False
            raise subprocess.TimeoutExpired(cmd=CHROME, timeout=timeout)
        self.running = False
        return 0


@pytest.fixture
def chrome(monkeypatch, tmp_path):
    """Stub the launcher and CDP client so no real Chrome is started."""
    state = SimpleNamespace(launches=[], cdp=None, launch=None)

    def fake_launch(args, profile, stderr, timeout):
        state.launches.append(list(args))
        if state.launch is not None:
            return state.launch(args, stderr)
        return "9222", DEVTOOLS_PATH

    def fake_cdp(url, timeout=30.0):
        state.cdp = FakeCDP(url, timeout)
        return state.cdp

    monkeypatch.setattr(browser_module, "launch_chrome", fake_launch)
    monkeypatch.setattr(browser_module, "CDPClient", fake_cdp)
    return state


@pytest.fixture
def profile(tmp_path) -> str:
    path = tmp_path / "profile"
    path.mkdir()
    return str(path)


def _browser(profile: str, **kwargs) -> Browser:
    return Browser(path=CHROME, profile=profile, **kwargs)


def test_missing_binary_raises_with_install_hint(monkeypatch, profile):
    monkeypatch.setattr(browser_module, "locate_chrome_binary", lambda: None)

    with pytest.raises(BrowserError, match="No Chrome, Chromium"):
        Browser(profile=profile)


def test_uses_located_binary_when_no_path_given(monkeypatch, chrome, profile):
    monkeypatch.setattr(browser_module, "locate_chrome_binary", lambda: CHROME)

    browser = Browser(profile=profile)

    assert browser.path == CHROME
    assert chrome.launches[0][0] == CHROME
    browser.close()


def test_connects_to_the_published_devtools_endpoint(chrome, profile):
    browser = _browser(profile, timeout=7.0)

    assert chrome.cdp.url == f"ws://127.0.0.1:9222{DEVTOOLS_PATH}"
    assert chrome.cdp.timeout == 7.0
    assert browser.cdp is chrome.cdp
    browser.close()


def test_headless_native_user_agent_is_rewritten(chrome, profile):
    browser = _browser(profile)

    assert chrome.cdp.user_agent == "Mozilla/5.0 (X11) Chrome/120.0 Safari"
    browser.close()


def test_user_agent_without_headless_token_is_left_alone(chrome, profile):
    browser = _browser(profile)
    chrome.cdp.native_user_agent = "Mozilla/5.0 (X11) Chrome/120.0 Safari"

    browser._apply_user_agent_fixup()

    assert chrome.cdp.user_agent == "Mozilla/5.0 (X11) Chrome/120.0 Safari"
    browser.close()


def test_sandbox_failure_is_retried_with_no_sandbox(chrome, profile):
    def sandbox_then_ok(args, stderr):
        if "--no-sandbox" in args:
            return "9222", DEVTOOLS_PATH
        stderr.write(b"No usable sandbox: set --no-sandbox")
        raise BrowserError("Chrome exited at once")

    chrome.launch = sandbox_then_ok

    browser = _browser(profile)

    assert len(chrome.launches) == 2
    assert "--no-sandbox" in chrome.launches[1]
    assert browser.cdp is chrome.cdp
    browser.close()


def test_other_launch_failures_are_not_retried(chrome, profile):
    def fail(args, stderr):
        raise BrowserError("Chrome exited at once")

    chrome.launch = fail

    with pytest.raises(BrowserError, match="exited at once"):
        _browser(profile)

    assert len(chrome.launches) == 1


def test_connection_error_is_wrapped_and_cleans_up(monkeypatch, chrome, profile):
    def broken_cdp(url, timeout=30.0):
        raise ConnectionRefusedError("port closed")

    monkeypatch.setattr(browser_module, "CDPClient", broken_cdp)

    with pytest.raises(BrowserError, match="DevTools port: port closed"):
        _browser(profile)


def test_version_reports_product_string(chrome, profile):
    browser = _browser(profile)

    assert browser.version() == "Chrome/120.0"
    browser.close()


def test_context_passes_only_the_proxy_server(chrome, profile):
    browser = _browser(profile)

    context_id = browser.context("http://user:pw@proxy.example:8080")

    assert context_id == "ctx-1"
    method, params = chrome.cdp.calls[-1]
    assert method == "Target.createBrowserContext"
    assert params == {
        "disposeOnDetach": True,
        "proxyServer": "http://proxy.example:8080",
    }
    browser.close()


def test_context_without_proxy_has_no_proxy_server(chrome, profile):
    browser = _browser(profile)

    browser.context()

    assert chrome.cdp.calls[-1][1] == {"disposeOnDetach": True}
    browser.close()


def test_dispose_sends_the_context_id(chrome, profile):
    browser = _browser(profile)

    browser.dispose("ctx-1")

    assert chrome.cdp.calls[-1] == (
        "Target.disposeBrowserContext",
        {"browserContextId": "ctx-1"},
    )
    browser.close()


@pytest.mark.parametrize("error", [CDPError("gone"), OSError("socket closed")])
def test_dispose_ignores_a_dead_connection(chrome, profile, error):
    browser = _browser(profile)

    def dead(method, params=None):
        raise error

    browser.cdp.call = dead

    browser.dispose("ctx-1")
    browser.close()


def test_kill_terminates_the_process_immediately(chrome, profile):
    browser = _browser(profile)
    browser.proc = FakeProcess()

    browser.kill()

    assert browser.proc.events == ["kill"]
    browser.close()


def test_close_terminates_a_running_process(chrome, profile):
    browser = _browser(profile)
    browser.proc = FakeProcess()

    browser.close()

    assert browser.proc.events == ["terminate", "wait"]
    assert chrome.cdp.closed


def test_close_escalates_to_kill_when_terminate_hangs(chrome, profile):
    browser = _browser(profile)
    browser.proc = FakeProcess(hang_once=True)

    browser.close()

    assert browser.proc.events == ["terminate", "wait", "kill", "wait"]


def test_close_removes_an_owned_profile(chrome):
    browser = Browser(path=CHROME)
    owned = browser.profile
    assert os.path.isdir(owned)

    browser.close()

    assert not os.path.exists(owned)


def test_close_keeps_a_caller_supplied_profile(chrome, profile):
    browser = _browser(profile)

    browser.close()

    assert os.path.isdir(profile)


def test_close_is_safe_to_call_twice(chrome, profile):
    browser = _browser(profile)

    browser.close()
    browser.close()


def test_context_manager_closes_on_exit(chrome, profile):
    with _browser(profile) as browser:
        assert browser.cdp is chrome.cdp

    assert chrome.cdp.closed
