"""Find an installed Chrome/Chromium, or the downloaded headless shell.

Nothing here launches or talks to a browser; these are pure filesystem
and platform lookups. The order of preference is:

1. An explicit ``chrome`` path from settings.
2. A well-known binary for the current OS (``CANDIDATES``), skipping
   Ubuntu's snap Chromium (its private ``/tmp`` hides
   ``DevToolsActivePort``).
3. The headless shell previously downloaded into ``HOME`` by
   :mod:`app.services.browser.downloader`.

``None`` means nothing was found; callers turn that into a user-facing
error.
"""

import os
import platform
import shutil
import tempfile
import time

from .constants import (
    ARCH_ALIASES,
    CANDIDATES,
    CFT_PLATFORMS,
    CFT_VERSION,
    HOME_PATH,
    PROFILE_PREFIX,
)


def chrome_for_testing_platform(
    system: str | None = None, machine: str | None = None
) -> str | None:
    """Map the current OS/arch to a Chrome for Testing platform tag.

    :param system: Override for ``platform.system()``.
    :param machine: Override for ``platform.machine()``.
    :returns: One of ``"linux64"``, ``"linux-arm64"``, ``"mac-x64"``,
        ``"mac-arm64"``, ``"win64"``, or ``None`` if the pair is unknown.
    """
    resolved_system = system or platform.system()
    arch = ARCH_ALIASES.get((machine or platform.machine()).lower())
    if arch is None:
        return None
    return CFT_PLATFORMS.get((resolved_system, arch))


def headless_shell_path(plat: str | None = None) -> str:
    """Return the on-disk location of the downloaded headless shell.

    :param plat: Platform tag; defaults to the current platform, or the
        placeholder ``"none"`` if detection fails (the file then won't
        exist, so :func:`locate_chrome_binary` returns ``None``).
    :returns: An absolute path; the file may not exist.
    """
    plat = plat or chrome_for_testing_platform() or "none"
    exe = (
        "chrome-headless-shell.exe"
        if plat.startswith("win")
        else "chrome-headless-shell"
    )
    return os.path.join(
        HOME_PATH,
        "chrome",
        CFT_VERSION,
        f"chrome-headless-shell-{plat}",
        exe,
    )


def is_always_headless_shell(path: str | None) -> bool:
    """Return whether ``path`` is the always-headless shell binary.

    The shell ignores ``--headless=new`` (and warns), so :class:`Browser`
    must not pass it.

    :param path: A candidate binary path, possibly ``None``.
    :returns: True if the basename looks like the headless shell.
    """
    return "headless-shell" in os.path.basename(path or "")


def is_snap_binary(path: str) -> bool:
    """Return whether ``path`` resolves to Ubuntu's snap Chromium.

    Two checks, because the snap installs both a wrapper script and a
    real binary: the path (or its realpath) under ``/snap/``, and a
    ``#!`` shebang that mentions ``/snap/``.

    :param path: A candidate binary path.
    :returns: True if it is (or is a wrapper for) a snap Chromium.
    """
    if path.startswith("/snap/") or os.path.realpath(path).startswith("/snap/"):
        return True
    try:
        with open(path, "rb") as handle:
            head = handle.read(4096)
    except OSError:
        return False
    return head.startswith(b"#!") and b"/snap/" in head


def locate_chrome_binary() -> str | None:
    """Return a usable Chrome/Chromium path, or ``None``.

    Checks, in order: the ``chrome`` setting, the platform's known
    candidates, then the downloaded headless shell.

    :returns: An absolute path to an executable file, or ``None``.
    """
    for candidate in CANDIDATES.get(platform.system(), []):
        candidate = os.path.expandvars(candidate)
        if os.sep not in candidate:
            candidate = shutil.which(candidate) or ""
            if is_snap_binary(candidate):
                # The snap's private /tmp hides DevToolsActivePort.
                continue
        if os.path.isfile(candidate):
            return candidate
    shell = headless_shell_path()
    return shell if os.path.isfile(shell) else None


def prune_stale_profiles(max_age_days: float = 2) -> None:
    """Delete Chrome profiles older than ``max_age_days`` from the temp dir.

    Best-effort: profiles deleted between listing and removal, or held
    open by a still-running process, are left alone.

    :param max_age_days: Age in days after which a profile is removed.
    """
    root = tempfile.gettempdir()
    cutoff = time.time() - max_age_days * 86400
    try:
        names = [name for name in os.listdir(root) if name.startswith(PROFILE_PREFIX)]
    except OSError:
        return
    for name in names:
        path = os.path.join(root, name)
        try:
            if os.path.getmtime(path) < cutoff:
                shutil.rmtree(path, ignore_errors=True)
        except OSError:
            pass


def ensure_engine_home() -> None:
    """Create ``HOME_PATH`` with ``0700`` permissions.

    Re-applies the mode because ``makedirs(mode=…)`` is subject to the
    umask, so a directory created under a permissive umask could remain
    group/world-readable.
    """
    os.makedirs(HOME_PATH, mode=0o700, exist_ok=True)
    if os.name != "nt":
        os.chmod(HOME_PATH, 0o700)
