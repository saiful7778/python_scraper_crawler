import hashlib
import http.client
import io
import logging
import os
import zipfile

import pytest

from app.services.browser import downloader, locator
from app.services.browser.browser_error import BrowserError
from app.services.browser.constants import CFT_VERSION

PLAT = "linux64"


def _shell_archive(plat: str = PLAT) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(
            f"chrome-headless-shell-{plat}/chrome-headless-shell", b"#!/bin/sh\n"
        )
    return buffer.getvalue()


class FakeOpener:
    """Records the URL and returns a response body, or raises."""

    def __init__(self, body: bytes = b"", error: BaseException | None = None):
        self.body = body
        self.error = error
        self.urls: list[str] = []

    def __call__(self, url, timeout):
        self.urls.append(url)
        if self.error is not None:
            raise self.error
        return io.BytesIO(self.body)


@pytest.fixture
def engine_home(monkeypatch, tmp_path):
    home = tmp_path / ".engine"
    monkeypatch.setattr(locator, "HOME_PATH", str(home))
    return home


def _pin(monkeypatch, data: bytes, plat: str = PLAT) -> None:
    digest = hashlib.sha256(data).hexdigest()
    monkeypatch.setattr(downloader, "CFT_SHA256", {plat: digest})


def test_successful_download_installs_the_shell(monkeypatch, engine_home, caplog):
    data = _shell_archive()
    _pin(monkeypatch, data)
    opener = FakeOpener(data)

    with caplog.at_level(logging.INFO, logger=downloader.__name__):
        path = downloader.download_headless_shell(plat=PLAT, opener=opener)

    assert path == locator.headless_shell_path(PLAT)
    assert os.path.isfile(path)
    assert opener.urls == [downloader.CFT_URL.format(v=CFT_VERSION, p=PLAT)]
    assert "SHA-256 verified" in caplog.text


def test_download_removes_the_partial_archive_after_success(monkeypatch, engine_home):
    data = _shell_archive()
    _pin(monkeypatch, data)

    downloader.download_headless_shell(plat=PLAT, opener=FakeOpener(data))

    install_root = os.path.dirname(os.path.dirname(locator.headless_shell_path(PLAT)))
    assert not os.path.exists(os.path.join(install_root, f"{PLAT}.zip.part"))


def test_hash_mismatch_is_rejected_and_archive_deleted(monkeypatch, engine_home):
    data = _shell_archive()
    monkeypatch.setattr(downloader, "CFT_SHA256", {PLAT: "0" * 64})

    with pytest.raises(BrowserError, match="SHA-256"):
        downloader.download_headless_shell(plat=PLAT, opener=FakeOpener(data))

    assert not os.path.exists(locator.headless_shell_path(PLAT))
    install_root = os.path.dirname(os.path.dirname(locator.headless_shell_path(PLAT)))
    assert not os.path.exists(os.path.join(install_root, f"{PLAT}.zip.part"))


@pytest.mark.parametrize(
    "error",
    [
        OSError("network unreachable"),
        http.client.IncompleteRead(b""),
    ],
)
def test_network_failures_are_wrapped(monkeypatch, engine_home, error):
    _pin(monkeypatch, _shell_archive())

    with pytest.raises(BrowserError, match="Could not download Chrome for Testing"):
        downloader.download_headless_shell(plat=PLAT, opener=FakeOpener(error=error))


def test_corrupt_archive_is_reported_as_unpack_failure(monkeypatch, engine_home):
    data = b"this is not a zip file"
    _pin(monkeypatch, data)

    with pytest.raises(BrowserError, match="Could not unpack"):
        downloader.download_headless_shell(plat=PLAT, opener=FakeOpener(data))


def test_unsupported_platform_is_rejected_before_any_download(engine_home):
    opener = FakeOpener(_shell_archive())

    with pytest.raises(BrowserError, match="No Chrome for Testing build"):
        downloader.download_headless_shell(plat="freebsd", opener=opener)

    assert opener.urls == []
