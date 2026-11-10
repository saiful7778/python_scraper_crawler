"""Chrome control and website rendering, re-exported from one place.

Import the public names from this package, for example::

    from app.services.browser import Browser, BrowserTab, render_website

Internal helpers (constants, the CDP transport, and so on) stay in their
own modules.
"""

from .browser import Browser
from .browser_error import BrowserError
from .browser_session import BrowserSession
from .cdp_client import CDPClient
from .cdp_error import CDPError
from .downloader import download_headless_shell
from .locator import ensure_engine_home, locate_chrome_binary, prune_stale_profiles
from .proxy import ProxyAuthenticator, split_proxy_url
from .tab import BrowserTab, ProxyTab
from .website import (
    RenderResult,
    RenderStatus,
    check_renderer_available,
    merge_render_status,
    render_website,
)
from .ws_client import WebSocketClient, WebSocketError

__all__ = [
    "Browser",
    "BrowserError",
    "BrowserSession",
    "BrowserTab",
    "CDPClient",
    "CDPError",
    "ProxyAuthenticator",
    "ProxyTab",
    "RenderResult",
    "RenderStatus",
    "WebSocketClient",
    "WebSocketError",
    "check_renderer_available",
    "download_headless_shell",
    "ensure_engine_home",
    "locate_chrome_binary",
    "merge_render_status",
    "prune_stale_profiles",
    "render_website",
    "split_proxy_url",
]
