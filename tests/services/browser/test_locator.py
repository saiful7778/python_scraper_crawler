import os
import pathlib
import time
from types import SimpleNamespace

import pytest

from app.services.browser import locator
from app.services.browser.constants import CFT_VERSION, PROFILE_PREFIX


def _stub_platform(monkeypatch, system: str, machine: str) -> None:
    monkeypatch.setattr(
        locator,
        "platform",
        SimpleNamespace(system=lambda: system, machine=lambda: machine),
    )


@pytest.fixture
def engine_home(monkeypatch, tmp_path):
    home = tmp_path / ".engine"
    monkeypatch.setattr(locator, "HOME_PATH", str(home))
    return home


@pytest.mark.parametrize(
    ("system", "machine", "expected"),
    [
        ("Linux", "x86_64", "linux64"),
        ("Linux", "aarch64", "linux-arm64"),
        ("Darwin", "arm64", "mac-arm64"),
        ("Darwin", "x86_64", "mac-x64"),
        ("Windows", "AMD64", "win64"),
    ],
)
def test_chrome_for_testing_platform_maps_known_pairs(system, machine, expected):
    assert locator.chrome_for_testing_platform(system, machine) == expected


@pytest.mark.parametrize(
    ("system", "machine"),
    [("FreeBSD", "x86_64"), ("Linux", "riscv64"), ("Windows", "arm64-unknown")],
)
def test_chrome_for_testing_platform_returns_none_for_unknown_pairs(system, machine):
    assert locator.chrome_for_testing_platform(system, machine) is None


def test_headless_shell_path_is_under_engine_home(engine_home):
    path = locator.headless_shell_path("linux64")

    assert path == os.path.join(
        str(engine_home),
        "chrome",
        CFT_VERSION,
        "chrome-headless-shell-linux64",
        "chrome-headless-shell",
    )


def test_headless_shell_path_uses_exe_on_windows(engine_home):
    assert locator.headless_shell_path("win64").endswith("chrome-headless-shell.exe")


def test_headless_shell_path_uses_detected_platform_by_default(
    monkeypatch, engine_home
):
    _stub_platform(monkeypatch, "Linux", "x86_64")

    assert "chrome-headless-shell-linux64" in locator.headless_shell_path()


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("/opt/chrome/chrome-headless-shell", True),
        ("/usr/bin/chromium", False),
        (None, False),
    ],
)
def test_is_always_headless_shell(path, expected):
    assert locator.is_always_headless_shell(path) is expected


def test_is_snap_binary_detects_snap_prefix():
    assert locator.is_snap_binary("/snap/bin/chromium") is True


def test_is_snap_binary_detects_snap_wrapper_script(tmp_path):
    wrapper = tmp_path / "chromium"
    wrapper.write_text("#!/bin/sh\nexec /snap/chromium/current/chromium\n")

    assert locator.is_snap_binary(str(wrapper)) is True


def test_is_snap_binary_is_false_for_ordinary_binary(tmp_path):
    binary = tmp_path / "chrome"
    binary.write_bytes(b"\x7fELF...")

    assert locator.is_snap_binary(str(binary)) is False


def test_is_snap_binary_is_false_for_missing_file(tmp_path):
    assert locator.is_snap_binary(str(tmp_path / "missing")) is False


def _executable(path):
    path.write_text("#!/bin/sh\n")
    path.chmod(0o755)
    return str(path)


def test_locate_returns_first_candidate_found(monkeypatch, engine_home, tmp_path):
    _stub_platform(monkeypatch, "Linux", "x86_64")
    chromium = _executable(tmp_path / "chromium")
    found = {"chromium": chromium}
    monkeypatch.setattr(locator.shutil, "which", lambda name: found.get(name))

    assert locator.locate_chrome_binary() == chromium


def test_locate_skips_snap_chromium(monkeypatch, engine_home, tmp_path):
    _stub_platform(monkeypatch, "Linux", "x86_64")
    snap = tmp_path / "chromium-snap"
    snap.write_text("#!/bin/sh\nexec /snap/chromium/current/chromium\n")
    snap.chmod(0o755)
    real = _executable(tmp_path / "chromium-browser")
    found = {"chromium": str(snap), "chromium-browser": real}
    monkeypatch.setattr(locator.shutil, "which", lambda name: found.get(name))

    assert locator.locate_chrome_binary() == real


def test_locate_falls_back_to_downloaded_headless_shell(monkeypatch, engine_home):
    _stub_platform(monkeypatch, "Linux", "x86_64")
    monkeypatch.setattr(locator.shutil, "which", lambda name: None)
    shell = locator.headless_shell_path()
    os.makedirs(os.path.dirname(shell))
    _executable(pathlib.Path(shell))

    assert locator.locate_chrome_binary() == shell


def test_locate_returns_none_when_nothing_is_installed(monkeypatch, engine_home):
    _stub_platform(monkeypatch, "Linux", "x86_64")
    monkeypatch.setattr(locator.shutil, "which", lambda name: None)

    assert locator.locate_chrome_binary() is None


def test_prune_removes_only_old_profiles_with_prefix(monkeypatch, tmp_path):
    temp_root = tmp_path / "tmp"
    temp_root.mkdir()
    monkeypatch.setattr(locator.tempfile, "gettempdir", lambda: str(temp_root))
    old_profile = temp_root / f"{PROFILE_PREFIX}old"
    new_profile = temp_root / f"{PROFILE_PREFIX}new"
    unrelated = temp_root / "other-old"
    for directory in (old_profile, new_profile, unrelated):
        directory.mkdir()
        (directory / "file").write_text("x")
    three_days_ago = time.time() - 3 * 86400
    for directory in (old_profile, unrelated):
        os.utime(directory, (three_days_ago, three_days_ago))

    locator.prune_stale_profiles(max_age_days=2)

    assert not old_profile.exists()
    assert new_profile.exists()
    assert unrelated.exists()


def test_prune_ignores_missing_temp_directory(monkeypatch, tmp_path):
    monkeypatch.setattr(
        locator.tempfile, "gettempdir", lambda: str(tmp_path / "missing")
    )

    locator.prune_stale_profiles()


def test_ensure_engine_home_creates_private_directory(engine_home):
    locator.ensure_engine_home()

    assert engine_home.is_dir()
    if os.name != "nt":
        assert (engine_home.stat().st_mode & 0o777) == 0o700
