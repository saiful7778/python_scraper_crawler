import json
import queue
import threading
from typing import Any, Final, TypedDict
from .ws_client import WebSocketClient

CDP_DEAD = object()

#: JSON-compatible scalar/container used throughout CDP payloads.
JSONValue = Any

#: Parameters sent with a CDP call or received with an event.
Params = dict[str, JSONValue]

#: Result payload returned by a CDP call.
Result = dict[str, JSONValue]


#: A single CDP protocol message, on the wire or decoded.
class CDPMessage(TypedDict, total=False):
    id: int
    method: str
    params: Params
    sessionId: str
    result: Result
    error: dict[str, str]


class CDPError(Exception):
    """Raised when a CDP call fails, times out, or the connection is lost."""


class CDPClient:
    """Multiplex CDP calls and events over one WebSocket connection.

    A background thread reads messages. Replies are routed by ``id`` to
    the waiting caller; events are routed by ``(sessionId, method)`` to
    subscribers. When the reader dies (browser crash, network failure),
    every waiter is woken with :data:`CDP_DEAD` or an error reply so no
    call hangs.

    :param ws_url: The browser's WebSocket debugger URL.
    :param timeout: Default seconds allowed for a single :meth:`call`.
    """

    #: A sentinel queued onto subscribers when the reader dies.
    _DEAD: Final[object] = CDP_DEAD

    def __init__(self, ws_url: str, timeout: float = 30.0) -> None:
        #: Optional browser-wide user agent applied to each attached tab.
        #: Set externally before creating :class:`BrowserTab` instances.
        self.user_agent: str | None = None
        self.ws: WebSocketClient = WebSocketClient(ws_url, timeout=timeout)
        self.timeout: float = timeout

        self._next_id: int = 0
        self._id_lock: threading.Lock = threading.Lock()
        # id -> Queue(1) holding the one-and-only reply for that call
        self._pending: dict[int, "queue.Queue[CDPMessage]"] = {}
        # (session_id, method) -> list of subscriber queues
        self._subscriptions: dict[
            tuple[str | None, str], list["queue.Queue[Params | object]"]
        ] = {}
        self._subscriptions_lock: threading.Lock = threading.Lock()

        #: Set to the exception that killed the reader, or ``None``.
        self.closed: Exception | None = None

        self._reader: threading.Thread = threading.Thread(
            target=self._read_loop,
            name="cdp-reader",
            daemon=True,
        )
        self._reader.start()

    def _read_loop(self) -> None:
        """Read messages forever, routing replies and events to waiters.

        Any failure is recorded in :attr:`closed` and broadcast to every
        pending call and subscriber so they stop waiting.
        """
        try:
            while True:
                raw: str = self.ws.recv_text()
                message: CDPMessage = json.loads(raw)
                if "id" in message:
                    reply_queue: "queue.Queue[CDPMessage] | None" = self._pending.pop(
                        message["id"], None
                    )
                    if reply_queue is not None:
                        reply_queue.put(message)
                else:
                    key: tuple[str | None, str] = (
                        message.get("sessionId"),
                        message.get("method", ""),
                    )
                    with self._subscriptions_lock:
                        subscribers: tuple["queue.Queue[Params | object]", ...] = tuple(
                            self._subscriptions.get(key, ())
                        )
                    params: Params = message.get("params", {})
                    for subscriber in subscribers:
                        subscriber.put(params)
        except Exception as error:  # noqa: BLE001 -- recorded for waiters
            self.closed = error
            error_reply: CDPMessage = {
                "error": {"message": f"connection lost: {error}"}
            }
            for reply_queue in list(self._pending.values()):
                reply_queue.put(error_reply)
            with self._subscriptions_lock:
                all_subscribers: list["queue.Queue[Params | object]"] = [
                    q for qs in self._subscriptions.values() for q in qs
                ]
            for subscriber in all_subscribers:
                subscriber.put(self._DEAD)

    def call(
        self,
        method: str,
        params: Params | None = None,
        session_id: str | None = None,
        timeout: float | None = None,
    ) -> Result:
        """Invoke a CDP ``method`` and return its ``result`` dict.

        :param method: The CDP method name, e.g. ``"Page.navigate"``.
        :param params: Optional parameters dict for the method.
        :param session_id: Optional target session to scope the call to.
        :param timeout: Seconds to wait for a reply; defaults to the
            client's timeout.
        :returns: The ``result`` payload of the reply.
        :raises CDPError: If the reply contains an error, the wait times
            out, or the connection is lost.
        """
        if self.closed:
            raise CDPError(f"connection lost: {self.closed}")

        with self._id_lock:
            self._next_id += 1
            message_id: int = self._next_id

        reply_queue: "queue.Queue[CDPMessage]" = queue.Queue(1)
        self._pending[message_id] = reply_queue
        if self.closed:
            # The reader died after the check above and will never answer.
            self._pending.pop(message_id, None)
            raise CDPError(f"connection lost: {self.closed}")

        request: CDPMessage = {
            "id": message_id,
            "method": method,
            "params": params or {},
        }
        if session_id:
            request["sessionId"] = session_id

        try:
            self.ws.send_text(json.dumps(request))
        except OSError as error:
            # The browser died and the reader has not noticed yet.
            self._pending.pop(message_id, None)
            raise CDPError(f"{method}: connection lost: {error}") from error

        effective_timeout: float = timeout or self.timeout
        reply: CDPMessage
        try:
            reply = reply_queue.get(timeout=effective_timeout)
        except queue.Empty:
            self._pending.pop(message_id, None)
            raise CDPError(f"{method}: no reply within {effective_timeout} s") from None

        if "error" in reply:
            raise CDPError(f"{method}: {reply['error'].get('message')}")
        return reply.get("result", {})

    def subscribe_events(
        self,
        method: str,
        session_id: str | None = None,
        event_queue: "queue.Queue[Params | object] | None" = None,
    ) -> "queue.Queue[Params | object]":
        """Register for ``method`` events (optionally per ``session_id``).

        :param method: The event name, e.g. ``"Page.loadEventFired"``.
        :param session_id: Optional session to scope the subscription to.
        :param event_queue: Optional existing queue to receive events;
            otherwise a new one is created.
        :returns: The queue that will receive event parameter dicts (or
            :data:`CDP_DEAD` when the connection dies).
        """
        event_queue = event_queue or queue.Queue()
        key: tuple[str | None, str] = (session_id, method)
        with self._subscriptions_lock:
            self._subscriptions.setdefault(key, []).append(event_queue)
        return event_queue

    def unsubscribe_events(
        self,
        method: str,
        event_queue: "queue.Queue[Params | object]",
        session_id: str | None = None,
    ) -> None:
        """Remove a previously registered subscription, if still present.

        :param method: The event name used when subscribing.
        :param event_queue: The queue returned by :meth:`subscribe_events`.
        :param session_id: The session used when subscribing.
        """
        key: tuple[str | None, str] = (session_id, method)
        with self._subscriptions_lock:
            subscribers: list["queue.Queue[Params | object]"] = self._subscriptions.get(
                key, []
            )
            if event_queue in subscribers:
                subscribers.remove(event_queue)

    def wait_for_event(
        self,
        method: str,
        session_id: str | None = None,
        timeout: float | None = None,
        event_queue: "queue.Queue[Params | object] | None" = None,
    ) -> Params:
        """Block until one ``method`` event arrives, returning its params.

        :param method: The event name to wait for.
        :param session_id: Optional session to scope the wait to.
        :param timeout: Seconds to wait; defaults to the client timeout.
        :param event_queue: An existing subscription to consume from. When
            omitted, a temporary subscription is created and removed on
            return, so use this to wait on an event you already
            subscribed to (e.g. to avoid missing a fast event).
        :returns: The event's parameter dict.
        :raises CDPError: On timeout or if the connection is lost.
        """
        owns_subscription: bool = event_queue is None
        event_queue = event_queue or self.subscribe_events(method, session_id)
        if self.closed and event_queue.empty():
            event_queue.put(self._DEAD)
        event: Params | object
        try:
            event = event_queue.get(timeout=timeout or self.timeout)
        except queue.Empty:
            raise CDPError(
                f"no {method} event within {timeout or self.timeout} s"
            ) from None
        finally:
            if owns_subscription:
                self.unsubscribe_events(method, event_queue, session_id)
        if event is self._DEAD:
            raise CDPError(f"connection lost: {self.closed}")
        # Narrowing: the sentinel case was handled above.
        assert isinstance(event, dict)
        return event

    def close(self) -> None:
        """Close the underlying WebSocket."""
        self.ws.close()
