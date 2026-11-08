"""Fetch the JSON that backs a single Google Maps place page.

A place page issues one ``/maps/preview/place?`` JSON request; this
function subscribes to the Network domain, navigates, and returns that
response's body. It is the single-place analogue of
:class:`batches.BatchResponseReader`.
"""

from __future__ import annotations

import base64
import logging
import queue
import time
from typing import Final

from app.services.browser import BrowserTab, CDPError
from app.services.browser.cdp_client import Params
from app.utils.errors import describe

from .selectors import LEAD_PANEL_URL_PATTERN

logger = logging.getLogger(__name__)

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
            logger.warning("Google showed its unusual-traffic page for %s", url)
            return ""
        deadline = time.monotonic() + timeout
        request_id: str | None = None
        last_error: str = "no panel response was seen"
        while time.monotonic() < deadline:
            while request_id is None:
                try:
                    message = event_queue.get_nowait()
                except queue.Empty:
                    break
                if not isinstance(message, dict):
                    raise CDPError(
                        f"the panel request for {url}: connection lost: "
                        f"{describe(tab.client.closed) if tab.client.closed else 'closed'}"
                    )
                response: Params = message.get("response", {})
                if LEAD_PANEL_URL_PATTERN.match(response.get("url", "")) and "json" in (
                    response.get("mimeType") or ""
                ):
                    found = message.get("requestId")
                    if isinstance(found, str) and found:
                        request_id = found
            if request_id:
                try:
                    result = tab.call(
                        "Network.getResponseBody",
                        {"requestId": request_id},
                    )
                except CDPError as error:
                    # Still downloading; the error is kept for the timeout message.
                    if tab.client.closed:
                        raise
                    last_error = str(error)
                else:
                    return response_text(result, url)
            time.sleep(_POLL_INTERVAL_SECONDS)
        logger.warning(
            "no panel JSON for %s within %s s: %s", url, timeout, last_error
        )
        return ""
    finally:
        tab.client.unsubscribe_events(
            "Network.responseReceived", event_queue, tab.session_id
        )


def response_text(result: Params, url: str) -> str:
    """Return a ``Network.getResponseBody`` reply as text, decoding base64 when Chrome sends it.

    :raises CDPError: If the reply has no ``body`` or it cannot be decoded.
    """
    body = result.get("body")
    if not isinstance(body, str):
        raise CDPError(
            f"the response for {url} has no body; the reply had keys {sorted(result)}"
        )
    if not result.get("base64Encoded"):
        return body
    try:
        return base64.b64decode(body, validate=True).decode("utf-8")
    except (ValueError, UnicodeDecodeError) as error:
        raise CDPError(
            f"the base64 response for {url} cannot be decoded: {describe(error)}"
        ) from error
