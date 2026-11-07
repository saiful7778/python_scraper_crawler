"""Checks that keep website fetching away from private and internal addresses."""

import ipaddress
import socket

NAT64_NETWORK = ipaddress.ip_network("64:ff9b::/96")


def is_public_ip_address(address: str | int) -> bool:
    """Return True when ``address`` is a globally routable, non-multicast IP.

    IPv6 addresses that embed an IPv4 address (IPv4-mapped, 6to4, NAT64 and the
    old ``::a.b.c.d`` form) are judged by the embedded IPv4 address.
    """
    try:
        ip = ipaddress.ip_address(
            address.split("%")[0] if isinstance(address, str) else address
        )
    except ValueError:
        return False
    if ip.version == 6:
        embedded = ip.ipv4_mapped or ip.sixtofour
        if embedded is None and (ip in NAT64_NETWORK or 1 < int(ip) < 1 << 32):
            embedded = ipaddress.ip_address(int(ip) & 0xFFFFFFFF)
        if embedded is not None:
            ip = embedded
    return ip.is_global and not (ip.is_multicast or getattr(ip, "is_site_local", False))


def resolves_only_to_public_addresses(host: str) -> bool:
    """Return True when ``host`` resolves and every resolved address is public."""
    try:
        infos = socket.getaddrinfo(host, None)
    except (OSError, UnicodeError):
        return False
    return bool(infos) and all(is_public_ip_address(info[4][0]) for info in infos)


def create_public_connection(address, *args, **kwargs) -> socket.socket:
    """Open a TCP connection and refuse it if the peer is not a public address.

    Raises:
        OSError: when the connected peer address is not public.
    """
    connection = socket.create_connection(address, *args, **kwargs)
    if not is_public_ip_address(connection.getpeername()[0]):
        connection.close()
        raise OSError("refused a non-public address")
    return connection
