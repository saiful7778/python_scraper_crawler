"""The :class:`Browser` object: locate, launch, and connect to Chrome.

Orchestrates :mod:`.locator` (find the binary),
:mod:`.launcher` (spawn and discover the port), and
:mod:`.cdp_client` (talk to it). Owns the process and a private profile
directory, and cleans both up on :meth:`close`.
"""

import os
import shutil
import subprocess
import tempfile
import time
from typing import Final, Self

from .browser_error import BrowserError
from .cdp_client import CDPClient
from .cdp_error import CDPError
from .constants import PY
from .launcher import build_launch_args, launch_chrome
from .locator import (
    PROFILE_PREFIX,
    locate_chrome_binary,
)
from .proxy import split_proxy_url

#: Seconds between attempts to delete a profile Windows still has open.
_PROFILE_RETRY_COUNT: Final[int] = 10
_PROFILE_RETRY_INTERVAL: Final[float] = 0.3

#: Seconds to wait for ``SIGTERM`` to take effect before escalating.
_TERMINATE_GRACE_SECONDS: Final[float] = 5.0


class Browser:
    """A running Chrome process and its CDP connection.

    :param path: A Chrome/Chromium binary; defaults to what
        :func:`locate_chrome_binary` finds.
    :param headless: Request headless mode. Ignored for the
        always-headless shell.
    :param profile: A directory to use as the Chrome profile. A temporary
        one is created and deleted on :meth:`close` when omitted.
    :param lang: The ``--lang`` value.
    :param timeout: Seconds allowed for each startup phase.
    :raises BrowserError: If no binary is found, or startup fails. On
        failure the process and profile are cleaned up before the error
        propagates.
    """

    def __init__(
        self,
        path: str | None = None,
        headless: bool = True,
        profile: str | None = None,
        lang: str = "en-US",
        timeout: float = 30.0,
    ) -> None:
        self.path = path or locate_chrome_binary()
        if not self.path:
            raise BrowserError(
                f"No Chrome, Chromium, Edge or Brave found. Run "
                f"`{PY} cli/engine.py start`, which downloads one, or "
                "install Google Chrome."
            )
        self._owns_profile = profile is None
        self.profile = profile or tempfile.mkdtemp(prefix=PROFILE_PREFIX)
        self._stderr = open(  # noqa: SIM115 -- closed in _finalize
            os.path.join(self.profile, "chrome-stderr.log"), "w+b"
        )

        self.proc: subprocess.Popen | None = None
        self.cdp: CDPClient | None = None
        args = build_launch_args(
            self.path,
            self.profile,
            headless=headless,
            lang=lang,
        )
        try:
            try:
                port, ws_path = launch_chrome(args, self.profile, self._stderr, timeout)
            except BrowserError:
                # Ubuntu 23.10+ (AppArmor) blocks the sandbox for a
                # browser not installed under /opt, which includes the
                # downloaded shell. Retry once without the sandbox, as
                # Playwright does; it only ever loads Google Maps.
                self._stderr.seek(0)
                if (
                    b"No usable sandbox" not in self._stderr.read()
                    or "--no-sandbox" in args
                ):
                    raise
                port, ws_path = launch_chrome(
                    args + ["--no-sandbox"],
                    self.profile,
                    self._stderr,
                    timeout,
                )
            self.cdp = CDPClient(f"ws://127.0.0.1:{port}{ws_path}", timeout=timeout)
            self._apply_user_agent_fixup()
        except BrowserError:
            self.close()
            raise
        except Exception as error:
            self.close()
            raise BrowserError(
                f"Could not connect to Chrome's DevTools port: {error}"
            ) from error

    def _apply_user_agent_fixup(self) -> None:
        """Rewrite ``HeadlessChrome/`` in the native UA, if present.

        Google serves a reduced Maps UI to any browser whose UA says
        ``HeadlessChrome``. Replacing just that token (OS and version
        unchanged) gets the full view. Each :class:`BrowserTab` applies
        this UA before its first request. A window's UA has no
        ``HeadlessChrome`` and is left alone.
        """
        assert self.cdp is not None
        native = self.cdp.call("Browser.getVersion")["userAgent"]
        if "HeadlessChrome/" in native:
            self.cdp.user_agent = native.replace("HeadlessChrome/", "Chrome/")

    def version(self) -> str:
        """Return Chrome's ``product`` string, e.g. ``"Chrome/120.0"``."""
        assert self.cdp is not None
        return self.cdp.call("Browser.getVersion").get("product", "")

    def context(self, proxy: str | None = None) -> str:
        """Create an isolated browser context (like incognito).

        :param proxy: Optional proxy URL; only the server part is set on
            the context. Credentials are handled separately by
            :class:`ProxyAuthenticator`.
        :returns: The context id.
        """
        assert self.cdp is not None
        params: dict = {"disposeOnDetach": True}
        if proxy:
            params["proxyServer"] = split_proxy_url(proxy)[0]
        return self.cdp.call("Target.createBrowserContext", params)["browserContextId"]

    def dispose(self, context_id: str) -> None:
        """Dispose of a browser context, ignoring a dead connection."""
        assert self.cdp is not None
        try:
            self.cdp.call(
                "Target.disposeBrowserContext",
                {"browserContextId": context_id},
            )
        except (CDPError, OSError):
            pass

    def kill(self) -> None:
        """Kill the Chrome process immediately, ignoring errors."""
        try:
            assert self.proc is not None
            self.proc.kill()
        except (OSError, AttributeError):
            pass

    def close(self) -> None:
        """Close the CDP connection, stop Chrome, and clean up.

        Terminates the process (escalating to ``kill`` after a grace
        period) and deletes the profile if :class:`Browser` created it.
        Safe to call more than once.
        """
        if self.cdp:
            try:
                self.cdp.close()
            except Exception:  # noqa: BLE001, S110 -- best effort
                pass
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(_TERMINATE_GRACE_SECONDS)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(_TERMINATE_GRACE_SECONDS)
        self._finalize()

    def _finalize(self) -> None:
        """Close the stderr log and delete an owned profile."""
        self._stderr.close()
        if not self._owns_profile:
            return
        for _ in range(_PROFILE_RETRY_COUNT):
            # Windows keeps the profile locked for a moment after exit.
            shutil.rmtree(self.profile, ignore_errors=True)
            if not os.path.exists(self.profile):
                break
            time.sleep(_PROFILE_RETRY_INTERVAL)

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()
