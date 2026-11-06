"""Launch a Chrome process and discover its DevTools port.

``launch_chrome`` starts the process with the given arguments and polls
for the ``DevToolsActivePort`` file Chrome writes into its profile
directory. ``describe_launch_failure`` turns Chrome's stderr into a
message that names the actual problem (missing shared libraries, a
fatal error, or the last few stderr lines).
"""

import os
import platform
import subprocess
import time
from collections.abc import Sequence
from typing import IO, Final

from app.services.browser.browser_error import BrowserError

from .constants import APT_LINE

#: Seconds between polls for ``DevToolsActivePort``.
_PORT_POLL_INTERVAL: Final[float] = 0.1


def describe_launch_failure(binary_path: str, exit_code: int, stderr: str) -> str:
    """Turn Chrome's stderr into a human-readable failure message.

    :param binary_path: The binary that failed to launch.
    :param exit_code: Its exit code.
    :param stderr: Everything Chrome wrote to stderr.
    :returns: A message naming the cause when one can be identified,
        else a generic exit-code line.
    """
    missing_libraries = [
        line.strip()
        for line in stderr.splitlines()
        if "error while loading shared libraries" in line
    ]
    if missing_libraries:
        return (
            f"Chrome cannot start: {missing_libraries[0]}\n"
            "  Install the libraries it needs (Debian or Ubuntu):\n"
            f"    {APT_LINE}"
        )
    lines = [line.strip() for line in stderr.strip().splitlines() if line.strip()]
    # Chrome prints a stack trace after the FATAL line; keep just that.
    fatal = [line for line in lines if "FATAL" in line]
    last = fatal[0] if fatal else "; ".join(lines[-3:])
    return f"Chrome exited at once (code {exit_code}): {binary_path}" + (
        f": {last}" if last else ""
    )


def launch_chrome(
    args: Sequence[str],
    profile_dir: str,
    stderr_sink: IO[bytes],
    timeout: float,
) -> tuple[str, str]:
    """Start Chrome and wait for it to publish its DevTools port.

    ``DevToolsActivePort`` is written into ``profile_dir`` once Chrome is
    ready; its two whitespace-separated tokens are the port and the
    WebSocket path for the browser endpoint.

    :param args: The full command line (binary + flags); ``about:blank``
        is appended so Chrome reaches the "ready" state promptly.
    :param profile_dir: The ``--user-data-dir`` being used.
    :param stderr_sink: A writable binary file capturing Chrome's
        stderr, for :func:`describe_launch_failure`.
    :param timeout: Seconds allowed before giving up.
    :returns: ``(port, websocket_path)``.
    :raises BrowserError: If Chrome exits, or the port is not published
        in time.
    """
    stderr_sink.seek(0)
    stderr_sink.truncate()
    active_port_file = os.path.join(profile_dir, "DevToolsActivePort")
    _unlink_quietly(active_port_file)

    popen_kwargs: dict = {
        "stdout": subprocess.DEVNULL,
        "stderr": stderr_sink,
        "stdin": subprocess.DEVNULL,
    }
    if os.name == "nt":
        popen_kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW

    try:
        process = subprocess.Popen(list(args) + ["about:blank"], **popen_kwargs)
    except OSError as error:
        raise BrowserError(f"Could not start Chrome ({args[0]}): {error}") from error

    deadline = time.time() + timeout
    while time.time() < deadline:
        if process.poll() is not None:
            stderr_sink.seek(0)
            raise BrowserError(
                describe_launch_failure(
                    args[0],
                    process.returncode,
                    stderr_sink.read().decode("utf-8", "replace"),
                )
            )
        try:
            with open(active_port_file, encoding="utf-8") as handle:
                port, websocket_path = handle.read().split()
            return port, websocket_path
        except (OSError, ValueError):
            time.sleep(_PORT_POLL_INTERVAL)

    # The caller needs the process handle to terminate it on failure.
    process.kill()
    raise BrowserError(
        f"Chrome did not open its DevTools port within {timeout} s: " f"{args[0]}"
    )


def build_launch_args(
    binary_path: str,
    profile_dir: str,
    *,
    headless: bool,
    lang: str,
) -> list[str]:
    """Assemble the Chrome command line for scraping.

    :param binary_path: The Chrome/Chromium executable.
    :param profile_dir: A directory to use as ``--user-data-dir``.
    :param headless: Whether to request headless mode. Ignored when the
        binary is the always-headless shell.
    :param lang: The ``--lang`` value.
    :returns: The full argument list, without the trailing
        ``about:blank`` (added by :func:`launch_chrome`).
    """
    from app.services.browser.locator import is_always_headless_shell

    args: list[str] = [
        binary_path,
        "--remote-debugging-port=0",
        f"--user-data-dir={profile_dir}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-extensions",
        "--disable-background-networking",
        "--disable-sync",
        "--mute-audio",
        "--disable-component-update",
        "--disable-domain-reliability",
        "--disable-breakpad",
        "--metrics-recording-only",
        "--no-pings",
        "--disable-client-side-phishing-detection",
        "--disable-blink-features=AutomationControlled",
        "--window-size=1280,900",
        f"--lang={lang}",
        "--disable-dev-shm-usage",
    ]
    if headless and not is_always_headless_shell(binary_path):
        args.append("--headless=new")
    elif not headless:
        # Keep the real window out of sight.
        args.append("--window-position=-3000,-3000")
    if platform.system() == "Linux" and hasattr(os, "geteuid") and os.geteuid() == 0:
        # Chrome refuses to run as root with its sandbox (containers).
        args.append("--no-sandbox")
    return args


def _unlink_quietly(path: str) -> None:
    """Remove ``path`` if it exists, ignoring errors."""
    try:
        os.remove(path)
    except OSError:
        pass
