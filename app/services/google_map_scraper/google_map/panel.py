"""Fetch the JSON that backs a single Google Maps place page.

A place page issues one ``/maps/preview/place?`` JSON request; this
function subscribes to the Network domain, navigates, and returns that
response's body. It is the single-place analogue of
:class:`batches.BatchResponseReader`.
"""

from __future__ import annotations

import queue
import time
from typing import Final

from app.services.browser.cdp_client import Params
from app.services.browser.cdp_error import CDPError
from app.services.browser.tab import BrowserTab

from .selectors import LEAD_PANEL_URL_PATTERN

#: How often the response queue is drained while waiting.
_POLL_INTERVAL_SECONDS: Final[float] = 0.3


def fetch_lead_panel_json(tab: BrowserTab, url: str, timeout: float = 10.0) -> str:
    """Return the JSON body behind a single place page.

    Subscribes to ``Network.responseReceived``, navigates, and returns
    the body of the first ``/maps/preview/place?`` JSON response seen.

    :param tab: The tab to navigate.
    :param url: A single place URL (see
        :data:`selectors.LEAD_PANEL_URL_PATTERN`).
    :param timeout: Seconds to wait for the response.
    :returns: The JSON body, or ``""`` on timeout or when Google blocks
        the request with a ``/sorry/`` page.
    :raises CDPError: If the connection to the browser is lost.
    """
    event_queue: queue.Queue[Params | object] = tab.client.subscribe_events(
        "Network.responseReceived", tab.session_id
    )
    try:
        tab.navigate(url)
        if "/sorry/" in (tab.get_url() or ""):
            return ""
        deadline = time.time() + timeout
        request_id: str | None = None
        while time.time() < deadline:
            while request_id is None:
                try:
                    message = event_queue.get_nowait()
                except queue.Empty:
                    break
                if not isinstance(message, dict):
                    raise CDPError(f"connection lost: {tab.client.closed}")
                response: Params = message.get("response", {})
                if LEAD_PANEL_URL_PATTERN.match(response.get("url", "")) and "json" in (
                    response.get("mimeType") or ""
                ):
                    request_id = message.get("requestId", "")
            if request_id:
                try:
                    return tab.call(
                        "Network.getResponseBody",
                        {"requestId": request_id},
                    )["body"]
                except CDPError:
                    # Still downloading.
                    if tab.client.closed:
                        raise
            time.sleep(_POLL_INTERVAL_SECONDS)
        return ""
    finally:
        tab.client.unsubscribe_events(
            "Network.responseReceived", event_queue, tab.session_id
        )
