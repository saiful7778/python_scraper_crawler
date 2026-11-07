"""Open TCP connections only to public IP addresses."""

import socket
import time
from collections.abc import Callable

from .constants import CONNECT_TIMEOUT_SECONDS


def open_public_connection(
    host: str,
    port: int,
    is_public_ip: Callable[[str], bool],
    deadline: float,
) -> socket.socket:
    """Connect to ``host:port``, refusing any address that is not public.

    Every resolved address must be public, and the address actually
    connected to is checked again, which stops DNS rebinding.

    :param deadline: ``time.monotonic()`` value that caps each connect attempt.
    :raises OSError: If the destination is not public or no address answers.
    """
    infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    if not infos or not all(is_public_ip(str(info[4][0])) for info in infos):
        raise OSError("non-public destination")
    for family, kind, proto, _, address in infos:
        peer = socket.socket(family, kind, proto)
        try:
            peer.settimeout(
                min(CONNECT_TIMEOUT_SECONDS, max(0.1, deadline - time.monotonic()))
            )
            peer.connect(address)
            if not is_public_ip(peer.getpeername()[0]):
                raise OSError("non-public peer")
            return peer
        except OSError:
            peer.close()
    raise OSError("destination unavailable")
