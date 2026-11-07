import pytest

from app.utils.proxies import ProxyRotation, mask_proxy_credentials, parse_proxy_urls


@pytest.mark.parametrize("value", [None, ""])
def test_parse_proxy_urls_empty_value_is_no_proxies(value: object) -> None:
    assert parse_proxy_urls(value) == []


def test_parse_proxy_urls_strips_and_keeps_valid_urls() -> None:
    value = [" http://1.2.3.4:8080 ", "https://proxy.example:443", "socks5h://h:1080"]
    assert parse_proxy_urls(value) == [
        "http://1.2.3.4:8080",
        "https://proxy.example:443",
        "socks5h://h:1080",
    ]


def test_parse_proxy_urls_accepts_credentials_on_http_proxy() -> None:
    assert parse_proxy_urls(["http://user:pass@h:8080"]) == ["http://user:pass@h:8080"]


@pytest.mark.parametrize(
    "value",
    [
        "http://1.2.3.4:8080",
        ["http://a:1"] * 101,
        [123],
        ["ftp://h:21"],
        ["http://:8080"],
        ["http://h:99999"],
        ["socks5://u:p@h:1080"],
    ],
)
def test_parse_proxy_urls_rejects_invalid_values(value: object) -> None:
    with pytest.raises(ValueError, match="invalid prox"):
        parse_proxy_urls(value)


def test_mask_proxy_credentials_hides_password() -> None:
    assert mask_proxy_credentials("http://user:pass@h:8080") == "http://user:***@h:8080"


def test_mask_proxy_credentials_hides_user_without_password() -> None:
    assert mask_proxy_credentials("http://user@h:8080") == "http://***@h:8080"


def test_mask_proxy_credentials_keeps_url_without_credentials() -> None:
    assert mask_proxy_credentials("http://h:8080") == "http://h:8080"


def test_mask_proxy_credentials_fully_masks_malformed_url() -> None:
    assert mask_proxy_credentials("http://[bad") == "***"


def test_rotation_picks_in_turn_and_advances() -> None:
    rotation = ProxyRotation(["a", "b", "c"])
    assert len(rotation) == 3
    assert rotation.pick(0) == "a"
    assert rotation.pick(1) == "b"
    rotation.advance()
    assert rotation.pick(0) == "b"


def test_rotation_skips_failed_proxies() -> None:
    rotation = ProxyRotation(["a", "b", "c"])
    rotation.advance()
    rotation.mark_failed("b")
    assert rotation.pick(0) == "c"
    assert rotation.healthy_proxies() == ["a", "c"]


def test_rotation_uses_in_turn_proxy_when_all_failed() -> None:
    rotation = ProxyRotation(["a", "b"])
    rotation.mark_failed("a")
    rotation.mark_failed("b")
    assert rotation.pick(0) == "a"
    assert rotation.healthy_proxies() == ["a", "b"]


def test_rotation_without_proxies() -> None:
    rotation = ProxyRotation([])
    assert len(rotation) == 0
    assert rotation.pick(0) is None
    assert rotation.healthy_proxies() == []


def test_rotation_counts_distinct_proxies() -> None:
    rotation = ProxyRotation(["a", "a", "b"])
    assert len(rotation) == 3
    assert rotation.distinct_count == 2
