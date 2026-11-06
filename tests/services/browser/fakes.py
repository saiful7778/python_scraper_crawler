import json
import queue
import time
from collections.abc import Callable
from typing import Any


Responder = Callable[[dict[str, Any]], list[Any]]


class FakeWebSocket:
    """In-memory stand-in for WebSocketClient.

    Messages the client sends are decoded and handed to ``responder``, which
    returns the messages the "browser" should send back.
    """

    def __init__(self) -> None:
        self.incoming: queue.Queue[str | BaseException] = queue.Queue()
        self.sent: list[dict[str, Any]] = []
        self.responder: Responder = lambda request: []
        self.send_error: OSError | None = None
        self.closed = False

    def push(self, message: dict[str, Any] | BaseException) -> None:
        if isinstance(message, BaseException):
            self.incoming.put(message)
        else:
            self.incoming.put(json.dumps(message))

    def send_text(self, text: str) -> None:
        if self.send_error is not None:
            raise self.send_error
        request: dict[str, Any] = json.loads(text)
        self.sent.append(request)
        for message in self.responder(request):
            self.push(message)

    def recv_text(self) -> str:
        item = self.incoming.get()
        if isinstance(item, BaseException):
            raise item
        return item

    def close(self) -> None:
        self.closed = True
        self.incoming.put(OSError("closed by test"))


class ScriptedBrowser:
    """Answers CDP requests from a table keyed by method name.

    Each reply value is either a body dict (``{"result": ...}`` or
    ``{"error": ...}``) or a callable taking the request and returning one.
    Events in ``events`` are sent right after the reply to that method.
    """

    def __init__(self, fake: FakeWebSocket) -> None:
        self.replies: dict[str, Any] = {
            "Target.createTarget": {"result": {"targetId": "T1"}},
            "Target.attachToTarget": {"result": {"sessionId": "S1"}},
        }
        self.events: dict[str, list[dict[str, Any]]] = {}
        self.calls: list[dict[str, Any]] = []
        fake.responder = self._respond

    def _respond(self, request: dict[str, Any]) -> list[dict[str, Any]]:
        self.calls.append(request)
        method: str = request["method"]
        reply: Any = self.replies.get(method, {"result": {}})
        if callable(reply):
            reply = reply(request)
        messages: list[dict[str, Any]] = [{"id": request["id"], **reply}]
        messages.extend(self.events.get(method, []))
        return messages

    def methods(self) -> list[str]:
        return [request["method"] for request in self.calls]


def sequence(*replies: Any) -> Callable[[dict[str, Any]], Any]:
    """Return replies in order, repeating the last one once exhausted."""
    index = 0

    def _next(_request: dict[str, Any]) -> Any:
        nonlocal index
        reply = replies[min(index, len(replies) - 1)]
        index += 1
        return reply

    return _next


def wait_until(predicate: Callable[[], bool], timeout: float = 2.0) -> None:
    deadline = time.time() + timeout
    while not predicate():
        if time.time() > deadline:
            raise AssertionError("condition not met in time")
        time.sleep(0.01)
