import socket

import pytest

from app.utils import network_guard
from app.utils.network_guard import (
    create_public_connection,
    is_public_ip_address,
    resolves_only_to_public_addresses,
)


@pytest.mark.parametrize(
    "address",
    [
        "8.8.8.8",
        "2001:4860:4860::8888",
        "::ffff:8.8.8.8",
        "2002:0808:0808::",
        "64:ff9b::808:808",
        "::8.8.8.8",
        134744072,
    ],
)
def test_public_addresses_are_accepted(address: str | int) -> None:
    assert is_public_ip_address(address) is True


@pytest.mark.parametrize(
    "address",
    [
        "10.0.0.1",
        "127.0.0.1",
        "192.168.1.1",
        "224.0.0.1",
        "::1",
        "::ffff:10.0.0.1",
        "fe80::1%eth0",
        "not-an-ip",
        "",
    ],
)
def test_non_public_addresses_are_rejected(address: str) -> None:
    assert is_public_ip_address(address) is False


def _info(address: str) -> tuple:
    return (socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 0))


def test_resolves_only_to_public_addresses_when_all_are_public(monkeypatch) -> None:
    monkeypatch.setattr(
        network_guard.socket, "getaddrinfo", lambda host, port: [_info("8.8.8.8")]
    )
    assert resolves_only_to_public_addresses("dns.example") is True


def test_resolves_only_to_public_addresses_rejects_mixed_results(monkeypatch) -> None:
    monkeypatch.setattr(
        network_guard.socket,
        "getaddrinfo",
        lambda host, port: [_info("8.8.8.8"), _info("10.0.0.1")],
    )
    assert resolves_only_to_public_addresses("mixed.example") is False


def test_resolves_only_to_public_addresses_rejects_failed_or_empty_lookup(
    monkeypatch,
) -> None:
    def fail(host, port):
        raise socket.gaierror("no such host")

    monkeypatch.setattr(network_guard.socket, "getaddrinfo", fail)
    assert resolves_only_to_public_addresses("missing.example") is False

    monkeypatch.setattr(network_guard.socket, "getaddrinfo", lambda host, port: [])
    assert resolves_only_to_public_addresses("empty.example") is False


class _FakeSocket:
    def __init__(self, peer: str):
        self._peer = peer
        self.closed = False

    def getpeername(self) -> tuple[str, int]:
        return (self._peer, 80)

    def close(self) -> None:
        self.closed = True


def test_create_public_connection_returns_public_peer(monkeypatch) -> None:
    sock = _FakeSocket("8.8.8.8")
    monkeypatch.setattr(network_guard.socket, "create_connection", lambda *a, **k: sock)
    assert create_public_connection(("dns.example", 80)) is sock
    assert sock.closed is False


def test_create_public_connection_refuses_private_peer(monkeypatch) -> None:
    sock = _FakeSocket("192.168.0.5")
    monkeypatch.setattr(network_guard.socket, "create_connection", lambda *a, **k: sock)
    with pytest.raises(OSError, match="refused a non-public address"):
        create_public_connection(("internal.example", 80))
    assert sock.closed is True
