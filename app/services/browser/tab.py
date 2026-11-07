import queue
import time

from .cdp_client import CDPClient, JSONValue, Params, Result
from .cdp_error import CDPError


class BrowserTab:
    """A single Chrome tab, backed by a CDP target and session.

    Creates an ``about:blank`` target, attaches to it, enables the
    ``Page``, ``Runtime`` and ``Network`` domains, applies the client's
    user agent if set, and then navigates to ``url`` (so the first real
    network request sees the configured agent).

    :param client: The connection to the browser.
    :param url: The initial URL; ``"about:blank"`` skips navigation.
    :param context: Optional browser context id to create the target in.
    :raises CDPError: If target creation, attachment, or navigation fails.
        The target is closed before the error propagates.
    """

    def __init__(
        self,
        client: CDPClient,
        url: str = "about:blank",
        context: str | None = None,
    ) -> None:
        self.client: CDPClient = client
        create_params: Params = {"url": "about:blank"}
        if context:
            create_params["browserContextId"] = context
        self.target_id: str = client.call("Target.createTarget", create_params)[
            "targetId"
        ]
        try:
            self.session_id: str = client.call(
                "Target.attachToTarget",
                {"targetId": self.target_id, "flatten": True},
            )["sessionId"]
            for domain in ("Page", "Runtime", "Network"):
                client.call(f"{domain}.enable", session_id=self.session_id)
            if client.user_agent:
                self.call(
                    "Emulation.setUserAgentOverride",
                    {"userAgent": client.user_agent},
                )
            if url != "about:blank":
                self.navigate(url)
        except Exception:
            # Never leave a tab behind that nobody holds.
            if not client.closed:
                try:
                    client.call("Target.closeTarget", {"targetId": self.target_id})
                except Exception:  # noqa: BLE001, S110 -- re-raising original
                    pass
            raise

    def call(
        self,
        method: str,
        params: Params | None = None,
        timeout: float | None = None,
    ) -> Result:
        """Invoke a CDP method scoped to this tab's session.

        :param method: The CDP method name.
        :param params: Optional method parameters.
        :param timeout: Seconds to wait; defaults to the client timeout.
        :returns: The reply's ``result`` dict.
        :raises CDPError: On protocol error, timeout, or lost connection.
        """
        return self.client.call(method, params, self.session_id, timeout)

    def set_secure_cookie(self, name: str, value: str, domain: str) -> None:
        """Set a ``Secure``, path-``/`` cookie on ``domain`` for this tab.

        :param name: The cookie name.
        :param value: The cookie value.
        :param domain: The cookie domain, e.g. ``".example.com"``.
        """
        self.call(
            "Network.setCookie",
            {
                "name": name,
                "value": value,
                "domain": domain,
                "path": "/",
                "secure": True,
            },
        )

    def navigate(self, url: str, timeout: float = 45.0) -> None:
        """Navigate to ``url`` and wait for the page's load event.

        The load-event subscription is installed *before* navigating so a
        very fast load cannot be missed.

        :param url: The URL to navigate to.
        :param timeout: Seconds allowed for both the navigate reply and
            the subsequent load event.
        :raises CDPError: If navigation is rejected or the load event does
            not arrive in time.
        """
        load_queue: queue.Queue[Params | object] = self.client.subscribe_events(
            "Page.loadEventFired", self.session_id
        )
        try:
            result: Result = self.call("Page.navigate", {"url": url}, timeout=timeout)
            if result.get("errorText"):
                raise CDPError(f"navigate {url}: {result['errorText']}")
            self.client.wait_for_event(
                "Page.loadEventFired",
                self.session_id,
                timeout,
                event_queue=load_queue,
            )
        finally:
            self.client.unsubscribe_events(
                "Page.loadEventFired", load_queue, self.session_id
            )

    def evaluate(self, expression: str, timeout: float | None = None) -> JSONValue:
        """Evaluate ``expression`` in the page and return its value.

        The expression is run with ``awaitPromise`` and ``returnByValue``,
        so ``async`` expressions resolve and results must be JSON-able.

        :param expression: JavaScript source to evaluate.
        :param timeout: Seconds to wait; defaults to the client timeout.
        :returns: The JavaScript value, converted from JSON.
        :raises CDPError: If the expression throws, times out, or the
            connection is lost.
        """
        result: Result = self.call(
            "Runtime.evaluate",
            {
                "expression": expression,
                "returnByValue": True,
                "awaitPromise": True,
            },
            timeout,
        )
        if "exceptionDetails" in result:
            details: dict[str, JSONValue] = result["exceptionDetails"]
            description: str | None = details.get("exception", {}).get(
                "description"
            ) or details.get("text")
            raise CDPError(f"JavaScript: {description}")
        return result.get("result", {}).get("value")

    def get_url(self) -> str:
        """Return the tab's current ``location.href``."""
        return self.evaluate("location.href")

    def wait_for_truthy(
        self,
        expression: str,
        timeout: float = 15.0,
        interval: float = 0.25,
    ) -> JSONValue:
        """Poll ``expression`` until it yields a truthy value or times out.

        Evaluation errors are retried (the page may be mid-navigation) as
        long as the connection is alive. If *every* attempt errored, that
        error is re-raised; otherwise the timeout returns ``None``.

        :param expression: JavaScript source to evaluate repeatedly.
        :param timeout: Seconds to keep polling.
        :param interval: Seconds to sleep between attempts.
        :returns: The first truthy value, or ``None`` on timeout after at
            least one successful (falsy) evaluation.
        :raises CDPError: If the connection is lost, or if every attempt
            raised.
        """
        deadline: float = time.time() + timeout
        ever_evaluated: bool = False
        last_error: CDPError | None = None
        while time.time() < deadline:
            value: JSONValue
            try:
                value = self.evaluate(expression)
                ever_evaluated = True
            except CDPError as error:
                if self.client.closed:
                    raise
                value, last_error = None, error
            if value:
                return value
            time.sleep(interval)
        if last_error is not None and not ever_evaluated:
            raise last_error
        return None

    def close(self) -> None:
        """Close the tab's target, ignoring errors from a dead connection."""
        try:
            self.client.call("Target.closeTarget", {"targetId": self.target_id})
        except (CDPError, OSError):
            # A dead socket fails in call() before the reader notices.
            pass
