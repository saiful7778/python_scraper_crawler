import base64
import hashlib
import socket
import struct
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Self

import pytest

from app.services.browser.ws_client import (
    WEBSOCKET_GUID,
    WebSocketClient,
    WebSocketError,
)

Script = Callable[[socket.socket], None]


def _accept_for(head: bytes) -> str:
    key = next(
        line.split(b":", 1)[1].strip()
        for line in head.split(b"\r\n")
        if line.lower().startswith(b"sec-websocket-key:")
    )
    return base64.b64encode(
        hashlib.sha1(key + WEBSOCKET_GUID.encode()).digest()
    ).decode()


def _upgrade_response(head: bytes) -> bytes:
    return (
        "HTTP/1.1 101 Switching Protocols\r\n"
        "Upgrade: websocket\r\nConnection: Upgrade\r\n"
        f"Sec-WebSocket-Accept: {_accept_for(head)}\r\n\r\n"
    ).encode()


def _frame(opcode: int, payload: bytes, fin: bool = True) -> bytes:
    """Build an unmasked server-to-client frame."""
    header = bytes([(0x80 if fin else 0) | opcode])
    length = len(payload)
    if length < 126:
        header += bytes([length])
    elif length < 65536:
        header += bytes([126]) + struct.pack(">H", length)
    else:
        header += bytes([127]) + struct.pack(">Q", length)
    return header + payload


def _recv_exact(conn: socket.socket, count: int) -> bytes:
    data = b""
    while len(data) < count:
        chunk = conn.recv(count - len(data))
        if not chunk:
            raise ConnectionError("client closed the connection")
        data += chunk
    return data


def _read_client_frame(conn: socket.socket) -> tuple[int, bytes, bool]:
    """Read one client-to-server frame, returning (opcode, payload, was_masked)."""
    byte0, byte1 = _recv_exact(conn, 2)
    length = byte1 & 0x7F
    if length == 126:
        length = struct.unpack(">H", _recv_exact(conn, 2))[0]
    elif length == 127:
        length = struct.unpack(">Q", _recv_exact(conn, 8))[0]
    masked = bool(byte1 & 0x80)
    mask = _recv_exact(conn, 4) if masked else b"\0\0\0\0"
    data = _recv_exact(conn, length)
    return byte0 & 0x0F, bytes(b ^ mask[i & 3] for i, b in enumerate(data)), masked


class _Server:
    """Accepts one TCP connection on localhost and performs a scripted exchange."""

    def __init__(
        self,
        script: Script | None = None,
        handshake: Callable[[bytes], bytes] = _upgrade_response,
    ) -> None:
        self.listener = socket.create_server(("127.0.0.1", 0))
        self.port: int = self.listener.getsockname()[1]
        self.script = script
        self.handshake = handshake
        self.request_head: bytes = b""
        self.error: BaseException | None = None
        self.thread = threading.Thread(target=self._serve, daemon=True)

    @property
    def url(self) -> str:
        return f"ws://127.0.0.1:{self.port}/devtools/page/1"

    def _serve(self) -> None:
        try:
            conn, _ = self.listener.accept()
            with conn:
                head = b""
                while b"\r\n\r\n" not in head:
                    chunk = conn.recv(4096)
                    if not chunk:
                        return
                    head += chunk
                self.request_head = head
                conn.sendall(self.handshake(head))
                if self.script is not None:
                    self.script(conn)
        except BaseException as error:  # noqa: BLE001 -- surfaced in __exit__
            self.error = error
        finally:
            self.listener.close()

    def __enter__(self) -> Self:
        self.thread.start()
        return self

    def __exit__(self, exc_type: type[BaseException] | None, *_: object) -> None:
        self.thread.join(5)
        if self.error is not None and exc_type is None:
            raise self.error


@contextmanager
def connected(script: Script | None = None) -> Iterator[WebSocketClient]:
    with _Server(script) as server:
        client = WebSocketClient(server.url, timeout=5)
        try:
            yield client
        finally:
            client.close()


def test_rejects_non_ws_scheme():
    with pytest.raises(WebSocketError, match="only ws:// is supported, not wss://"):
        WebSocketClient("wss://example.com/devtools/browser", timeout=1)


def test_handshake_sends_a_websocket_upgrade_request():
    with _Server() as server:
        client = WebSocketClient(server.url, timeout=5)
        try:
            head = server.request_head.decode()
        finally:
            client.close()

    assert head.startswith("GET /devtools/page/1 HTTP/1.1\r\n")
    assert f"Host: 127.0.0.1:{server.port}" in head
    assert "Upgrade: websocket" in head
    assert "Sec-WebSocket-Version: 13" in head


def test_handshake_refused_by_server_raises():
    with (
        _Server(handshake=lambda _: b"HTTP/1.1 403 Forbidden\r\n\r\n") as server,
        pytest.raises(WebSocketError, match="handshake refused: HTTP/1.1 403"),
    ):
        WebSocketClient(server.url, timeout=5)


def test_handshake_with_wrong_accept_key_raises():
    def wrong_accept(_: bytes) -> bytes:
        return (
            b"HTTP/1.1 101 Switching Protocols\r\n"
            b"Sec-WebSocket-Accept: not-the-right-key\r\n\r\n"
        )

    with (
        _Server(handshake=wrong_accept) as server,
        pytest.raises(WebSocketError, match="bad Sec-WebSocket-Accept"),
    ):
        WebSocketClient(server.url, timeout=5)


def test_connection_closed_during_handshake_raises():
    with (
        _Server(handshake=lambda _: b"") as server,
        pytest.raises(WebSocketError, match="closed during the websocket handshake"),
    ):
        WebSocketClient(server.url, timeout=5)


def test_send_text_is_masked_and_carries_utf8():
    received: list[tuple[int, bytes, bool]] = []

    def script(conn: socket.socket) -> None:
        received.append(_read_client_frame(conn))

    with connected(script) as client:
        client.send_text("héllo")

    assert received == [(0x1, "héllo".encode(), True)]


def test_recv_text_returns_a_text_frame():
    def script(conn: socket.socket) -> None:
        conn.sendall(_frame(0x1, b'{"id": 1}'))

    with connected(script) as client:
        assert client.recv_text() == '{"id": 1}'


def test_recv_text_decodes_binary_frames_as_utf8():
    def script(conn: socket.socket) -> None:
        conn.sendall(_frame(0x2, "binäry".encode()))

    with connected(script) as client:
        assert client.recv_text() == "binäry"


def test_recv_text_reassembles_fragmented_message():
    def script(conn: socket.socket) -> None:
        conn.sendall(_frame(0x1, b"hel", fin=False) + _frame(0x0, b"lo"))

    with connected(script) as client:
        assert client.recv_text() == "hello"


def test_recv_text_handles_16_and_64_bit_payload_lengths():
    short_text = "a" * 300
    long_text = "b" * 70000

    def script(conn: socket.socket) -> None:
        conn.sendall(_frame(0x1, short_text.encode()) + _frame(0x1, long_text.encode()))

    with connected(script) as client:
        assert client.recv_text() == short_text
        assert client.recv_text() == long_text


def test_ping_is_answered_with_matching_pong_and_skipped():
    pongs: list[tuple[int, bytes, bool]] = []

    def script(conn: socket.socket) -> None:
        conn.sendall(_frame(0x9, b"ping-data"))
        pongs.append(_read_client_frame(conn))
        conn.sendall(_frame(0x1, b"after"))

    with connected(script) as client:
        assert client.recv_text() == "after"

    assert pongs == [(0xA, b"ping-data", True)]


def test_close_frame_from_server_raises():
    def script(conn: socket.socket) -> None:
        conn.sendall(_frame(0x8, struct.pack(">H", 1000)))

    with (
        connected(script) as client,
        pytest.raises(WebSocketError, match="closed by the server"),
    ):
        client.recv_text()


def test_unexpected_opcode_raises():
    def script(conn: socket.socket) -> None:
        conn.sendall(_frame(0x3, b"x"))

    with (
        connected(script) as client,
        pytest.raises(WebSocketError, match="unexpected websocket opcode 3"),
    ):
        client.recv_text()


def test_recv_raises_when_peer_disconnects():
    with connected() as client, pytest.raises(WebSocketError, match="websocket closed"):
        client.recv_text()


def test_close_sends_normal_closure_frame_and_closes_socket():
    received: list[tuple[int, bytes, bool]] = []

    def script(conn: socket.socket) -> None:
        received.append(_read_client_frame(conn))

    with _Server(script) as server:
        client = WebSocketClient(server.url, timeout=5)
        client.close()

    assert received == [(0x8, struct.pack(">H", 1000), True)]
    assert client.sock.fileno() == -1
