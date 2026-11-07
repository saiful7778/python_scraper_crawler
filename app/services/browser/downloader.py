"""Download, verify, and unpack the Chrome for Testing headless shell.

The download is pinned by SHA-256 in :data:`app.constants.CFT_SHA256`.
The archive is streamed while hashing, verified before it is ever
unpacked, and removed on mismatch. Unpacking happens in a staging
directory and is moved into place with ``os.replace``, so a partially
unpacked shell is never visible to :func:`locate_chrome_binary`.
"""

import hashlib
import http.client
import os
import platform
import shutil
import tempfile
import urllib.request
import zipfile
from collections.abc import Callable
from contextlib import AbstractContextManager
from typing import BinaryIO, Final

from .browser_error import BrowserError
from .constants import CFT_SHA256, CFT_URL, CFT_VERSION
from .locator import (
    chrome_for_testing_platform,
    ensure_engine_home,
    headless_shell_path,
)

#: Chunk size for streaming the download (1 MiB).
_CHUNK_SIZE: Final[int] = 1 << 20

#: Seconds before the download is abandoned.
_DOWNLOAD_TIMEOUT: Final[float] = 60.0


def _unlink_quietly(path: str) -> None:
    """Remove ``path`` if it exists, ignoring errors."""
    try:
        os.remove(path)
    except OSError:
        pass


def download_headless_shell(
    log: Callable[[str], None] = print,
    plat: str | None = None,
    opener: Callable[..., AbstractContextManager[BinaryIO]] = urllib.request.urlopen,
) -> str:
    """Download, verify, and unpack Chrome for Testing's headless shell.

    :param log: Called with progress lines.
    :param plat: Platform tag; defaults to the current platform.
    :param opener: URL opener; injectable for tests.
    :returns: The path of the installed headless shell binary.
    :raises BrowserError: On an unsupported platform, a failed or
        truncated download, a SHA-256 mismatch, or a corrupt archive.
        On a mismatch or truncation, the temporary archive is deleted
        and never unpacked.
    """
    plat = plat or chrome_for_testing_platform()
    if plat not in CFT_SHA256:
        raise BrowserError(
            "No Chrome for Testing build exists for "
            f"{platform.system()} {platform.machine()}. "
            "Install Chromium, then set CHROME to its path."
        )
    url = CFT_URL.format(v=CFT_VERSION, p=plat)
    install_root = os.path.dirname(os.path.dirname(headless_shell_path(plat)))
    ensure_engine_home()
    os.makedirs(install_root, exist_ok=True)
    archive = os.path.join(install_root, f"{plat}.zip.part")
    log(
        f"▶ No Chrome found, so downloading Chrome for Testing "
        f"{CFT_VERSION} (headless shell, {plat}, about 100 MB) once into "
        f".engine/ from {url}"
    )

    digest = hashlib.sha256()
    try:
        with (
            opener(url, timeout=_DOWNLOAD_TIMEOUT) as response,
            open(archive, "wb") as handle,
        ):
            for chunk in iter(lambda: response.read(_CHUNK_SIZE), b""):
                digest.update(chunk)
                handle.write(chunk)
    except (
        OSError,
        http.client.HTTPException,
    ) as error:  # a cut connection raises IncompleteRead, not OSError
        _unlink_quietly(archive)
        raise BrowserError(
            f"Could not download Chrome for Testing from {url}: {error}"
        ) from error

    if digest.hexdigest() != CFT_SHA256[plat]:
        _unlink_quietly(archive)
        raise BrowserError(
            "The Chrome download does not match the SHA-256 pinned in "
            "engine/browser.py, so it was deleted and not run. Try again "
            "later, or install Google Chrome."
        )

    staging = tempfile.mkdtemp(prefix="unpack-", dir=install_root)
    try:
        with zipfile.ZipFile(archive) as archive_zip:
            for member in archive_zip.infolist():
                # zipfile drops absolute paths and ".." parts, so this is
                # safe against zip-slip.
                extracted = archive_zip.extract(member, staging)
                mode = member.external_attr >> 16
                if mode & 0o111 and not member.is_dir():
                    # zipfile loses exec bits on some OSes; restore them.
                    os.chmod(extracted, 0o755)
        final = os.path.dirname(headless_shell_path(plat))
        shutil.rmtree(final, ignore_errors=True)
        # Atomic move: a half-unpacked shell never shows up in find.
        os.replace(os.path.join(staging, os.path.basename(final)), final)
    except (OSError, zipfile.BadZipFile) as error:
        raise BrowserError(f"Could not unpack the Chrome download: {error}") from error
    finally:
        shutil.rmtree(staging, ignore_errors=True)
        _unlink_quietly(archive)

    log("  ✓ SHA-256 verified, unpacked")
    return headless_shell_path(plat)
