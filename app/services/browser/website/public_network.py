"""Open TCP connections only to public IP addresses."""

import socket
import time
from collections.abc import Callable

from app.utils.errors import describe

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
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as error:
        raise OSError(f"could not resolve {host}:{port}: {describe(error)}") from error
    if not infos or not all(is_public_ip(str(info[4][0])) for info in infos):
        raise NonPublicDestinationError(f"{host} resolves to a non-public address")
    last_error: OSError | None = None
    for family, kind, proto, _, address in infos:
        peer = socket.socket(family, kind, proto)
        try:
            peer.settimeout(
                min(CONNECT_TIMEOUT_SECONDS, max(0.1, deadline - time.monotonic()))
            )
            peer.connect(address)
            if not is_public_ip(peer.getpeername()[0]):
                raise NonPublicDestinationError(
                    f"{host} connected to a non-public peer"
                )
            return peer
        except OSError as error:
            last_error = error
            peer.close()
    raise OSError(
        f"could not connect to {host}:{port} at any of {len(infos)} address(es): "
        f"{describe(last_error) if last_error else 'no address'}"
    ) from last_error


class NonPublicDestinationError(OSError):
    """The host resolves to, or connects to, an address that is not public."""
