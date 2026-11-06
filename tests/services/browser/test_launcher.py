import io
import os
from types import SimpleNamespace

import pytest

from app.services.browser import launcher
from app.services.browser.browser_error import BrowserError


class FakeProcess:
    def __init__(self, exit_code: int | None = None) -> None:
        self._exit_code = exit_code
        self.returncode = exit_code
        self.killed = False

    def poll(self) -> int | None:
        return self._exit_code

    def kill(self) -> None:
        self.killed = True


@pytest.fixture
def popen(monkeypatch):
    """Replace ``subprocess.Popen`` inside the launcher with a recorder."""
    state = SimpleNamespace(process=FakeProcess(), calls=[], on_start=None)

    def fake_popen(argv, **kwargs):
        state.calls.append((list(argv), kwargs))
        if state.on_start is not None:
            state.on_start(state)
        return state.process

    monkeypatch.setattr(
        launcher,
        "subprocess",
        SimpleNamespace(Popen=fake_popen, DEVNULL=-3),
    )
    return state


def _publish_port(profile_dir, port="9222", path="/devtools/browser/abc"):
    with open(os.path.join(profile_dir, "DevToolsActivePort"), "w") as handle:
        handle.write(f"{port}\n{path}\n")


def test_describe_failure_names_missing_shared_libraries():
    stderr = (
        "chrome: error while loading shared libraries: libnss3.so: "
        "cannot open shared object file"
    )

    message = launcher.describe_launch_failure("/usr/bin/chrome", 127, stderr)

    assert "libnss3.so" in message
    assert "apt-get install" in message


def test_describe_failure_prefers_the_fatal_line():
    stderr = "noise\nFATAL:browser_main.cc unexpected\n  at frame 1\n"

    message = launcher.describe_launch_failure("/usr/bin/chrome", 1, stderr)

    assert "FATAL:browser_main.cc unexpected" in message
    assert "code 1" in message


def test_describe_failure_falls_back_to_last_stderr_lines():
    stderr = "one\ntwo\nthree\nfour\n"

    message = launcher.describe_launch_failure("/usr/bin/chrome", 2, stderr)

    assert "two; three; four" in message
    assert "one" not in message


def test_describe_failure_without_stderr_gives_exit_code_only():
    message = launcher.describe_launch_failure("/usr/bin/chrome", 3, "")

    assert message == "Chrome exited at once (code 3): /usr/bin/chrome"


def test_launch_returns_port_and_websocket_path(popen, tmp_path):
    profile = str(tmp_path)
    popen.on_start = lambda state: _publish_port(profile, "9333", "/devtools/x")

    port, ws_path = launcher.launch_chrome(
        ["/usr/bin/chrome", "--flag"], profile, io.BytesIO(), timeout=2
    )

    assert (port, ws_path) == ("9333", "/devtools/x")
    argv, kwargs = popen.calls[0]
    assert argv == ["/usr/bin/chrome", "--flag", "about:blank"]
    assert kwargs["stdout"] == -3
    assert kwargs["stdin"] == -3


def test_launch_discards_a_stale_port_file(popen, tmp_path):
    _publish_port(str(tmp_path), "1111", "/stale")

    with pytest.raises(BrowserError, match="DevTools port"):
        launcher.launch_chrome(
            ["/usr/bin/chrome"], str(tmp_path), io.BytesIO(), timeout=0
        )


def test_launch_times_out_and_kills_the_process(popen, tmp_path):
    with pytest.raises(BrowserError, match="did not open its DevTools port"):
        launcher.launch_chrome(
            ["/usr/bin/chrome"], str(tmp_path), io.BytesIO(), timeout=0
        )

    assert popen.process.killed


def test_launch_reports_early_exit_with_stderr(popen, tmp_path):
    popen.process = FakeProcess(exit_code=1)
    sink = io.BytesIO()
    popen.on_start = lambda state: sink.write(b"FATAL:sandbox failed to start\n")

    with pytest.raises(BrowserError, match="FATAL:sandbox failed to start"):
        launcher.launch_chrome(["/usr/bin/chrome"], str(tmp_path), sink, timeout=2)


def test_launch_wraps_spawn_failure(popen, tmp_path):
    def missing(state):
        raise OSError("no such file")

    popen.on_start = missing

    with pytest.raises(BrowserError, match="Could not start Chrome"):
        launcher.launch_chrome(
            ["/missing/chrome"], str(tmp_path), io.BytesIO(), timeout=1
        )


def test_build_args_include_profile_language_and_headless_flag():
    args = launcher.build_launch_args(
        "/usr/bin/chrome", "/tmp/profile", headless=True, lang="fr-FR"
    )

    assert args[0] == "/usr/bin/chrome"
    assert "--user-data-dir=/tmp/profile" in args
    assert "--lang=fr-FR" in args
    assert "--headless=new" in args
    assert "--remote-debugging-port=0" in args


def test_build_args_hide_window_when_not_headless():
    args = launcher.build_launch_args(
        "/usr/bin/chrome", "/tmp/profile", headless=False, lang="en-US"
    )

    assert "--headless=new" not in args
    assert "--window-position=-3000,-3000" in args


def test_build_args_never_request_headless_for_headless_shell():
    args = launcher.build_launch_args(
        "/opt/chrome-headless-shell", "/tmp/profile", headless=True, lang="en-US"
    )

    assert "--headless=new" not in args


def test_build_args_adds_no_sandbox_when_running_as_root_on_linux(monkeypatch):
    monkeypatch.setattr(launcher, "platform", SimpleNamespace(system=lambda: "Linux"))
    monkeypatch.setattr(os, "geteuid", lambda: 0, raising=False)

    args = launcher.build_launch_args(
        "/usr/bin/chrome", "/tmp/profile", headless=True, lang="en-US"
    )

    assert "--no-sandbox" in args


def test_build_args_omit_no_sandbox_for_regular_user(monkeypatch):
    monkeypatch.setattr(launcher, "platform", SimpleNamespace(system=lambda: "Linux"))
    monkeypatch.setattr(os, "geteuid", lambda: 1000, raising=False)

    args = launcher.build_launch_args(
        "/usr/bin/chrome", "/tmp/profile", headless=True, lang="en-US"
    )

    assert "--no-sandbox" not in args
