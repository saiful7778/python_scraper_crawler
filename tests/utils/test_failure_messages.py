import pytest

from app.utils.failure_messages import is_proxy_failure, is_timeout_failure


@pytest.mark.parametrize(
    "message",
    [
        "net::ERR_PROXY_CONNECTION_FAILED",
        "net::ERR_TUNNEL_CONNECTION_FAILED",
        "Proxy refused the connection",
    ],
)
def test_is_proxy_failure_detects_proxy_errors(message: str) -> None:
    assert is_proxy_failure(message) is True


@pytest.mark.parametrize("message", ["connection lost", "Navigation timed out"])
def test_is_proxy_failure_ignores_other_errors(message: str) -> None:
    assert is_proxy_failure(message) is False


@pytest.mark.parametrize(
    "message",
    [
        "Navigation timed out after 45 s",
        "request timeout",
        "ERR_TIMED_OUT",
        "Timed Out",
    ],
)
def test_is_timeout_failure_detects_timeouts(message: str) -> None:
    assert is_timeout_failure(message) is True


@pytest.mark.parametrize("message", ["connection lost", "proxy refused the login"])
def test_is_timeout_failure_ignores_other_errors(message: str) -> None:
    assert is_timeout_failure(message) is False
