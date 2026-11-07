"""Proxy URL parsing and in-memory proxy authentication.

Chrome's CDP separates the proxy server URL from proxy credentials: the
server is set on the browser context, while credentials are supplied in
response to ``Fetch.authRequired`` events. :class:`ProxyAuthenticator`
installs a ``Fetch`` interceptor that answers those challenges once per
request and reports the proxy as ``refused`` if the same request is
challenged again.
"""

import queue
import threading
import urllib.parse

from .cdp_client import CDPError
from .tab import BrowserTab


class ProxyAuthenticator:
    """Answer Chrome's proxy auth challenges with a fixed credential.

    Registers a ``Fetch`` interceptor on a tab, continues all
    non-auth requests, and supplies credentials once per request that
    receives a proxy challenge. If the same request is challenged again,
    it is treated as a rejection: the request is cancelled and
    :attr:`refused` becomes true.

    :param tab: The tab to authenticate on.
    :param username: The proxy username.
    :param password: The proxy password.
    """

    def __init__(self, tab: BrowserTab, username: str, password: str) -> None:
        self.tab = tab
        self._username = username
        self._password = password
        # One queue for both events, so the reader sees them in order.
        self.event_queue: queue.Queue[dict | object] = tab.client.subscribe_events(
            "Fetch.requestPaused", tab.session_id
        )
        tab.client.subscribe_events(
            "Fetch.authRequired",
            tab.session_id,
            event_queue=self.event_queue,
        )
        self._done = threading.Event()
        self._asked: set[str] = set()
        #: True once the proxy rejected the supplied credentials.
        self.refused: bool = False
        tab.call(
            "Fetch.enable",
            {
                "handleAuthRequests": True,
                "patterns": [{"urlPattern": "*"}],
            },
        )
        threading.Thread(target=self._run, daemon=True, name="proxy-auth").start()

    def _run(self) -> None:
        """Read events until :meth:`close` is called or the tab dies."""
        while not self._done.is_set():
            try:
                event = self.event_queue.get(timeout=0.5)
            except queue.Empty:
                continue
            if not isinstance(event, dict):
                # The CDP connection died.
                return
            request_id = event.get("requestId")
            if not isinstance(request_id, str):
                continue
            try:
                self._handle(request_id, event)
            except CDPError:
                # The tab closed, or the request finished first.
                if self.tab.client.closed:
                    return

    def _handle(self, request_id: str, event: dict) -> None:
        """Dispatch a single ``Fetch`` event."""
        if "authChallenge" not in event:
            self.tab.call("Fetch.continueRequest", {"requestId": request_id})
            return
        if (
            event["authChallenge"].get("source") == "Proxy"
            and request_id not in self._asked
        ):
            # First challenge for this request: answer it.
            self._asked.add(request_id)
            self.tab.call(
                "Fetch.continueWithAuth",
                {
                    "requestId": request_id,
                    "authChallengeResponse": {
                        "response": "ProvideCredentials",
                        "username": self._username,
                        "password": self._password,
                    },
                },
            )
            return
        # Either not a proxy challenge, or the same request asked again:
        # treat the latter as a rejection.
        self.refused = self.refused or request_id in self._asked
        self.tab.call(
            "Fetch.continueWithAuth",
            {
                "requestId": request_id,
                "authChallengeResponse": {
                    "response": (
                        "CancelAuth" if request_id in self._asked else "Default"
                    )
                },
            },
        )

    def close(self) -> None:
        """Stop the reader and unsubscribe from the ``Fetch`` events."""
        self._done.set()
        for method in ("Fetch.requestPaused", "Fetch.authRequired"):
            self.tab.client.unsubscribe_events(
                method, self.event_queue, self.tab.session_id
            )


def split_proxy_url(url: str) -> tuple[str, str, str]:
    """Split a proxy URL into Chrome's server form and credentials.

    Chrome's ``proxyServer`` does not know ``socks5h`` (the DNS-at-proxy
    variant used by curl/urllib); it already resolves at the proxy, so
    the suffix is dropped. IPv6 hosts are bracketed.

    :param url: A proxy URL such as
        ``http://user:pass@proxy.example:8080`` or
        ``socks5h://proxy.example:1080``.
    :returns: ``(server_url, username, password)``, with credentials
        percent-decoded and possibly empty.
    """
    parsed = urllib.parse.urlsplit(url)
    hostname = parsed.hostname or ""
    host = f"[{hostname}]" if ":" in hostname else hostname
    scheme = "socks5" if parsed.scheme == "socks5h" else parsed.scheme
    server = f"{scheme}://{host}" + (f":{parsed.port}" if parsed.port else "")
    username = urllib.parse.unquote(parsed.username or "")
    password = urllib.parse.unquote(parsed.password or "")
    return server, username, password
