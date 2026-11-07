"""A loopback HTTP proxy that forwards only to public addresses.

Chrome is started with this proxy, so every request it makes goes
through :func:`open_public_connection`. Plain ``GET``/``HEAD`` requests
and ``CONNECT`` tunnels are both supported.
"""

import select
import threading
import time
import urllib.parse
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from socket import socket
from typing import cast

from .constants import (
    HOP_BY_HOP_HEADERS,
    PROXY_JOIN_SECONDS,
    RELAY_CHUNK_BYTES,
    RELAY_POLL_SECONDS,
)
from .public_network import open_public_connection


class PublicProxyHandler(BaseHTTPRequestHandler):
    """Serves one client: a ``CONNECT`` tunnel or a single plain HTTP request."""

    @property
    def _proxy(self) -> "PublicProxyServer":
        return cast("PublicProxyServer", self.server)

    def log_message(self, format: str, *args: object) -> None:
        pass

    def do_CONNECT(self) -> None:
        try:
            dest = urllib.parse.urlsplit("//" + self.path)
            if not dest.hostname or dest.username or dest.password or dest.path:
                raise ValueError("invalid destination")
            peer = self._connect(dest.hostname, dest.port or 443)
        except (OSError, ValueError, UnicodeError):
            self.send_error(403, "Destination unavailable")
            return
        try:
            self.send_response(200, "Connection established")
            self.end_headers()
            self._relay(peer)
        except OSError:
            pass
        finally:
            peer.close()

    def do_GET(self) -> None:
        try:
            dest = urllib.parse.urlsplit(self.path)
            if (
                dest.scheme != "http"
                or not dest.hostname
                or dest.username
                or dest.password
            ):
                raise ValueError("invalid destination")
            peer = self._connect(dest.hostname, dest.port or 80)
        except (OSError, ValueError, UnicodeError):
            self.send_error(403, "Destination unavailable")
            return
        try:
            peer.sendall(self._forwarded_request_head(dest))
            self._relay(peer)
        except OSError:
            peer.close()

    do_HEAD = do_GET

    def _connect(self, host: str, port: int) -> socket:
        return open_public_connection(
            host,
            port,
            self._proxy.is_public_ip,
            self._proxy.deadline,
        )

    def _forwarded_request_head(self, dest: urllib.parse.SplitResult) -> bytes:
        path = urllib.parse.urlunsplit(("", "", dest.path or "/", dest.query, ""))
        lines = [
            f"{self.command} {path} HTTP/1.1",
            f"Host: {dest.netloc}",
            "Connection: close",
        ]
        lines += [
            f"{name}: {value}"
            for name, value in self.headers.items()
            if name.lower() not in HOP_BY_HOP_HEADERS
        ]
        return ("\r\n".join(lines) + "\r\n\r\n").encode("latin-1")

    def _relay(self, peer: socket) -> None:
        with peer:
            while (
                not self._proxy.stopped.is_set()
                and time.monotonic() < self._proxy.deadline
            ):
                ready, _, _ = select.select(
                    [self.connection, peer], [], [], RELAY_POLL_SECONDS
                )
                for source in ready:
                    data = source.recv(RELAY_CHUNK_BYTES)
                    if not data:
                        return
                    target = peer if source is self.connection else self.connection
                    target.sendall(data)


class PublicProxyServer(ThreadingHTTPServer):
    """A loopback proxy server, started and stopped explicitly.

    :param is_public_ip: Predicate deciding whether an address is public.
    :param deadline: ``time.monotonic()`` value after which relays stop.
    """

    daemon_threads = True

    def __init__(self, is_public_ip: Callable[[str], bool], deadline: float) -> None:
        super().__init__(("127.0.0.1", 0), PublicProxyHandler)
        self.is_public_ip = is_public_ip
        self.deadline = deadline
        self.stopped = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> str:
        """Serve on a daemon thread and return the proxy URL for Chrome."""
        self._thread = threading.Thread(target=self.serve_forever, daemon=True)
        self._thread.start()
        return f"http://127.0.0.1:{self.server_port}"

    def stop(self) -> None:
        """Stop serving and wait briefly for the serving thread to exit."""
        self.stopped.set()
        self.shutdown()
        self.server_close()
        if self._thread is not None:
            self._thread.join(PROXY_JOIN_SECONDS)
