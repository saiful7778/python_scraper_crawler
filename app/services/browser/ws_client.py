import base64
import hashlib
import os
import socket
import struct
import threading
import urllib.parse

WEBSOCKET_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


class WebSocketError(OSError):
    """Raised when the WebSocket transport fails or the peer misbehaves."""


class WebSocketClient:
    """A minimal RFC 6455 WebSocket client (``ws://`` only).

    Only text frames are consumed by callers of :meth:`recv_text`; binary
    and fragmented frames are reassembled and decoded as UTF-8, which is
    all CDP ever sends. Pings are answered automatically; close frames
    raise :class:`WebSocketError`.

    The socket timeout is cleared after the handshake so the reader
    thread can block indefinitely on a quiet browser. Per-command
    timeouts live in :class:`ChromeDevToolsClient`.
    """

    def __init__(self, url: str, timeout: float = 30.0) -> None:
        """Open ``url`` (``ws://host[:port]/path``) and perform the upgrade.

        :param url: The WebSocket URL to connect to. Only ``ws://`` is
            supported; ``wss://`` would require TLS and is not implemented.
        :param timeout: Seconds allowed for the TCP connect and the
            HTTP upgrade handshake.
        :raises WebSocketError: On a non-``ws`` scheme, an empty or
            refused handshake, or a bad ``Sec-WebSocket-Accept``.
        """
        parsed: urllib.parse.ParseResult = urllib.parse.urlparse(url)
        if parsed.scheme != "ws":
            raise WebSocketError(f"only ws:// is supported, not {parsed.scheme}://")
        host: str | None = parsed.hostname
        port: int = parsed.port or 80
        self.sock: socket.socket = socket.create_connection(
            (host, port), timeout=timeout
        )
        self.sock.settimeout(timeout)
        self._send_lock: threading.Lock = threading.Lock()
        self._buffer: bytes = b""

        key: str = base64.b64encode(os.urandom(16)).decode()
        path: str = (parsed.path or "/") + (f"?{parsed.query}" if parsed.query else "")
        self.sock.sendall(
            (
                f"GET {path} HTTP/1.1\r\nHost: {host}:{port}\r\n"
                "Upgrade: websocket\r\nConnection: Upgrade\r\n"
                f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
            ).encode()
        )

        response_head: bytes = b""
        while b"\r\n\r\n" not in response_head:
            chunk: bytes = self.sock.recv(4096)
            if not chunk:
                raise WebSocketError("connection closed during the websocket handshake")
            response_head += chunk
        response_head, self._buffer = response_head.split(b"\r\n\r\n", 1)

        status_line: str
        header_block: str
        status_line, _, header_block = response_head.decode("latin-1").partition("\r\n")
        if " 101 " not in status_line:
            raise WebSocketError(f"websocket handshake refused: {status_line}")

        expected_accept: str = base64.b64encode(
            hashlib.sha1((key + WEBSOCKET_GUID).encode()).digest()
        ).decode()
        accept_values: list[str] = [
            value.strip()
            for name, _, value in (
                line.partition(":") for line in header_block.split("\r\n")
            )
            if name.strip().lower() == "sec-websocket-accept"
        ]
        if accept_values != [expected_accept]:
            raise WebSocketError("websocket handshake: bad Sec-WebSocket-Accept")

        # The reader blocks in recv() while the browser is quiet (slow page,
        # slow proxy); per-command timeouts are enforced in CDP.call instead.
        self.sock.settimeout(None)

    def _recv_exact(self, count: int) -> bytes:
        """Read exactly ``count`` bytes, buffering leftovers between calls.

        :param count: The exact number of bytes required.
        :returns: Exactly ``count`` bytes.
        :raises WebSocketError: If the peer closes mid-read.
        """
        while len(self._buffer) < count:
            chunk: bytes = self.sock.recv(max(65536, count - len(self._buffer)))
            if not chunk:
                raise WebSocketError("websocket closed")
            self._buffer += chunk
        head: bytes
        head, self._buffer = self._buffer[:count], self._buffer[count:]
        return head

    def _send_frame(self, opcode: int, payload: bytes) -> None:
        """Frame and send ``payload`` with ``opcode``, masking as clients must.

        :param opcode: The RFC 6455 opcode (e.g. ``0x1`` for text).
        :param payload: The raw frame body.
        :raises OSError: If the socket write fails.
        """
        mask: bytes = os.urandom(4)  # clients must mask (RFC 6455 5.3)
        length: int = len(payload)
        header: bytes = bytes([0x80 | opcode])
        if length < 126:
            header += bytes([0x80 | length])
        elif length < 65536:
            header += bytes([0x80 | 126]) + struct.pack(">H", length)
        else:
            header += bytes([0x80 | 127]) + struct.pack(">Q", length)
        masked: bytes = bytes(byte ^ mask[i & 3] for i, byte in enumerate(payload))
        with self._send_lock:
            self.sock.sendall(header + mask + masked)

    def send_text(self, text: str) -> None:
        """Send ``text`` as a single masked text frame.

        :param text: The UTF-8 text to send.
        :raises OSError: If the socket write fails.
        """
        self._send_frame(0x1, text.encode("utf-8"))

    def recv_text(self) -> str:
        """Return the next complete text message, reassembling fragments.

        Pings are answered transparently and pongs are ignored.

        :returns: The decoded text of one complete message.
        :raises WebSocketError: On a close frame, an unexpected opcode, or
            a lost connection.
        """
        parts: list[bytes] = []
        kind: int | None = None
        while True:
            byte0: int
            byte1: int
            byte0, byte1 = self._recv_exact(2)
            fin: int = byte0 & 0x80
            opcode: int = byte0 & 0x0F
            length: int = byte1 & 0x7F
            if length == 126:
                length = struct.unpack(">H", self._recv_exact(2))[0]
            elif length == 127:
                length = struct.unpack(">Q", self._recv_exact(8))[0]
            mask: bytes | None = self._recv_exact(4) if byte1 & 0x80 else None
            data: bytes = self._recv_exact(length)
            if mask:
                data = bytes(byte ^ mask[i & 3] for i, byte in enumerate(data))

            if opcode == 0x8:
                raise WebSocketError("websocket closed by the server")
            if opcode == 0x9:
                self._send_frame(0xA, data)
                continue
            if opcode == 0xA:
                continue
            if opcode in (0x1, 0x2):
                kind, parts = opcode, [data]
            elif opcode == 0x0 and kind is not None:
                parts.append(data)
            else:
                raise WebSocketError(f"unexpected websocket opcode {opcode}")

            if fin:
                message: bytes = b"".join(parts)
                kind, parts = None, []
                return message.decode("utf-8")

    def close(self) -> None:
        """Send a normal-close frame and close the socket, ignoring errors."""
        try:
            self._send_frame(0x8, struct.pack(">H", 1000))
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass
