"""Read Google Maps result-batch JSON bodies off the CDP Network domain.

The feed issues one JSON request per scroll; this class watches the three
Network events a batch goes through (request sent, response received,
loading failed), keeps track of which batches are in flight, and reads
each one's body once it is available. It never blocks on the network:
:meth:`BatchResponseReader.poll` drains whatever has arrived and returns
the newly parsed rows.
"""

import queue
from typing import Final

from app.services.browser.cdp_client import Params
from app.services.browser.cdp_error import CDPError
from app.services.browser.tab import BrowserTab
from app.services.google_map_scraper.extract_details import (
    PlaceRecord,
    parse_place_list,
)

from .selectors import BATCH_URL_PATTERN

#: After this many retries (one per poll), a batch whose body cannot be
#: read is dropped as evicted or failed. About 10 s at the collector's
#: 0.4 s polling cadence.
_MAX_BODY_READ_TRIES: Final[int] = 25


class BatchResponseReader:
    """Watch the ``Network`` domain and collect result-batch JSON bodies.

    Subscribes to ``Network.requestWillBeSent``,
    ``Network.responseReceived``, and ``Network.loadingFailed`` on the
    tab's session, using one shared queue so events arrive in order.
    Batches are read out with :meth:`poll`.
    """

    def __init__(self, tab: BrowserTab) -> None:
        """Subscribe to the three Network events on ``tab``'s session.

        :param tab: The tab whose network traffic should be watched.
        """
        self.tab: BrowserTab = tab
        # One queue for the three events a result batch goes through, so
        # poll() sees them in order.
        self.event_queue: queue.Queue[Params | object] = tab.client.subscribe_events(
            "Network.requestWillBeSent", tab.session_id
        )
        for method in (
            "Network.responseReceived",
            "Network.loadingFailed",
        ):
            tab.client.subscribe_events(
                method, tab.session_id, event_queue=self.event_queue
            )
        # requestId -> tries, for batches seen but not yet readable.
        self._pending: dict[str, int] = {}
        # Batch requests the page has sent and Google has not answered yet.
        self._inflight: set[str] = set()
        #: Batch requests the page sent in all.
        self.asked: int = 0
        #: Batches successfully read and parsed.
        self.count: int = 0
        #: Batch requests Google answered with 429 Too Many Requests.
        self.limited: int = 0

    @property
    def inflight(self) -> int:
        """Number of batch requests awaiting an answer."""
        return len(self._inflight)

    @property
    def pending(self) -> int:
        """Number of batches whose body is not readable yet."""
        return len(self._pending)

    def poll(self) -> list[PlaceRecord]:
        """Drain network events, read available batches, return new rows.

        Never blocks on the network. A batch whose body is not ready yet
        stays pending and is retried on the next call (up to
        ``_MAX_BODY_READ_TRIES``, then it is dropped as evicted/failed).

        :returns: The parsed lead rows from every batch newly read this
            call.
        :raises CDPError: If the connection to the browser is lost.
        """
        while True:
            try:
                message = self.event_queue.get_nowait()
            except queue.Empty:
                break
            if not isinstance(message, dict):
                # The connection died; the client puts a marker on every queue.
                raise CDPError(f"connection lost: {self.tab.client.closed}")
            request_id: str = message.get("requestId", "")
            if "request" in message:
                # requestWillBeSent: the page asked Google for the next batch.
                if BATCH_URL_PATTERN.match(message["request"].get("url", "")):
                    self._inflight.add(request_id)
                    self.asked += 1
                continue
            if "errorText" in message:
                # loadingFailed: no answer is coming for this one.
                self._inflight.discard(request_id)
                continue
            response: Params = message.get("response", {})
            if not BATCH_URL_PATTERN.match(response.get("url", "")):
                continue
            self._inflight.discard(request_id)
            if response.get("status") == 429:
                self.limited += 1
            elif "json" in (response.get("mimeType") or ""):
                self._pending[request_id] = 0

        rows: list[PlaceRecord] = []
        for request_id in list(self._pending):
            try:
                body: str = self.tab.call(
                    "Network.getResponseBody", {"requestId": request_id}
                )["body"]
            except CDPError:
                if self.tab.client.closed:
                    raise
                self._pending[request_id] += 1
                # Not finished yet; retry next time, for about 10 s.
                if self._pending[request_id] > _MAX_BODY_READ_TRIES:
                    del self._pending[request_id]
                continue
            del self._pending[request_id]
            self.count += 1
            rows += parse_place_list(body)
        return rows

    def close(self) -> None:
        """Unsubscribe from the three Network events."""
        for method in (
            "Network.requestWillBeSent",
            "Network.responseReceived",
            "Network.loadingFailed",
        ):
            self.tab.client.unsubscribe_events(
                method, self.event_queue, self.tab.session_id
            )
