import queue
from typing import Any


class FakeClock:
    """Stand-in for the ``time`` module: ``sleep`` advances ``time()``."""

    def __init__(self) -> None:
        self.now: float = 0.0

    def time(self) -> float:
        return self.now

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


class Script:
    """Replies handed out in order; the last one repeats.

    Exception instances are raised instead of returned.
    """

    def __init__(self, *items: Any) -> None:
        self.items: list[Any] = list(items)

    def next(self) -> Any:
        if not self.items:
            return None
        item = self.items.pop(0) if len(self.items) > 1 else self.items[0]
        if isinstance(item, BaseException):
            raise item
        return item


class FakeClient:
    """Records subscriptions; ``events`` is the inbox every subscriber shares."""

    def __init__(self) -> None:
        self.closed: Exception | None = None
        self.events: queue.Queue[Any] = queue.Queue()
        self.subscriptions: list[tuple[str, str, queue.Queue[Any]]] = []
        self.unsubscribed: list[tuple[str, queue.Queue[Any], str]] = []

    def subscribe_events(
        self,
        method: str,
        session_id: str,
        event_queue: queue.Queue[Any] | None = None,
    ) -> queue.Queue[Any]:
        event_queue = event_queue or self.events
        self.subscriptions.append((method, session_id, event_queue))
        return event_queue

    def unsubscribe_events(
        self,
        method: str,
        event_queue: queue.Queue[Any],
        session_id: str,
    ) -> None:
        self.unsubscribed.append((method, event_queue, session_id))


class FakeTab:
    """Scripted stand-in for BrowserTab; records every interaction."""

    def __init__(self, client: FakeClient | None = None) -> None:
        self.client: FakeClient = client or FakeClient()
        self.session_id: str = "S1"
        self.url: str = "https://www.google.com/maps"
        #: If set, navigation lands here instead of on the requested URL.
        self.redirect_to: str | None = None
        self.navigated: list[str] = []
        self.calls: list[tuple[str, Any]] = []
        self.replies: dict[str, Script] = {}
        self.evaluations: dict[str, Script] = {}
        self.evaluated: list[str] = []
        self.truthy: dict[str, Script] = {}
        self.waited: list[str] = []
        self.cookies: list[tuple[str, str, str]] = []

    def call(
        self,
        method: str,
        params: Any = None,
        timeout: float | None = None,
    ) -> Any:
        self.calls.append((method, params))
        if method in self.replies:
            return self.replies[method].next()
        return {}

    def navigate(self, url: str, timeout: float = 45.0) -> None:
        self.navigated.append(url)
        self.url = self.redirect_to or url

    def get_url(self) -> str:
        return self.url

    def evaluate(self, expression: str, timeout: float | None = None) -> Any:
        self.evaluated.append(expression)
        if expression in self.evaluations:
            return self.evaluations[expression].next()
        return None

    def wait_for_truthy(
        self,
        expression: str,
        timeout: float = 15.0,
        interval: float = 0.25,
    ) -> Any:
        self.waited.append(expression)
        if expression in self.truthy:
            return self.truthy[expression].next()
        return True

    def set_secure_cookie(self, name: str, value: str, domain: str) -> None:
        self.cookies.append((name, value, domain))
