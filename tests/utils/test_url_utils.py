import pytest

from app.utils.url_utils import encode_url_as_ascii, extract_site_host, is_junk_domain


def test_encode_url_as_ascii_percent_encodes_non_ascii_path_and_query() -> None:
    assert (
        encode_url_as_ascii("https://a.com/café?q=ü#frag")
        == "https://a.com/caf%C3%A9?q=%C3%BC"
    )


def test_encode_url_as_ascii_returns_ascii_url_unchanged() -> None:
    url = "https://a.com/x?y=1#z"
    assert encode_url_as_ascii(url) == url


def test_encode_url_as_ascii_returns_unparsable_url_unchanged() -> None:
    assert encode_url_as_ascii("http://[bad/café") == "http://[bad/café"


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://www.Studio.DE/path", "studio.de"),
        ("example.com", "example.com"),
        ("http://sub.shop.io:8080/x", "sub.shop.io"),
        ("", ""),
        ("http://[bad", ""),
    ],
)
def test_extract_site_host(url: str, expected: str) -> None:
    assert extract_site_host(url) == expected


@pytest.mark.parametrize(
    "domain",
    ["example.com", "shop.wixpress.com", "wixpress.com", "mail.yourdomain.io"],
)
def test_is_junk_domain_matches_default_junk(domain: str) -> None:
    assert is_junk_domain(domain) is True


@pytest.mark.parametrize("domain", ["studio.de", "myexample.com", "exampleshop.co.uk"])
def test_is_junk_domain_ignores_real_domains(domain: str) -> None:
    assert is_junk_domain(domain) is False


def test_is_junk_domain_with_custom_entries() -> None:
    assert is_junk_domain("x.bad.org", ("bad.org",)) is True
    assert is_junk_domain("bad.org.evil.com", ("bad.org",)) is False
    assert is_junk_domain("bad.example", ("bad",)) is True
