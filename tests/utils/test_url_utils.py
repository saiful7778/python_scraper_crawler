import pytest

from app.utils.url_utils import (
    encode_url_as_ascii,
    extract_site_host,
    is_junk_domain,
    normalize_url,
    same_scope,
    validate_url,
    website_candidates,
)


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


def test_normalize_url_adds_scheme_and_root_path() -> None:
    assert normalize_url("Shop.example") == "http://shop.example/"


def test_normalize_url_lowercases_scheme_and_host_and_drops_fragment() -> None:
    assert normalize_url("HTTPS://Shop.Example/Path?q=1#top") == "https://shop.example/Path?q=1"


def test_normalize_url_encodes_non_ascii() -> None:
    assert normalize_url("https://shop.example/café") == "https://shop.example/caf%C3%A9"


def test_normalize_url_drops_default_port_only() -> None:
    assert normalize_url("https://shop.example:443/x") == "https://shop.example/x"
    assert normalize_url("http://shop.example:443/x") == "http://shop.example:443/x"
    assert normalize_url("http://shop.example:8080/x") == "http://shop.example:8080/x"


def test_normalize_url_keeps_ipv6_brackets() -> None:
    assert normalize_url("http://[::1]:8080/") == "http://[::1]:8080/"


def test_validate_url_accepts_web_urls() -> None:
    validate_url("https://shop.example/")
    validate_url("http://shop.example")


def test_validate_url_rejects_credentials() -> None:
    with pytest.raises(ValueError):
        validate_url("https://user:pass@shop.example/")


@pytest.mark.parametrize("url", ["ftp://shop.example/", "file:///etc/passwd", "https:///path"])
def test_validate_url_rejects_non_web_urls(url: str) -> None:
    with pytest.raises(ValueError):
        validate_url(url)


def test_same_scope_ignores_www() -> None:
    assert same_scope("https://www.shop.example/a", "https://shop.example/") is True


def test_same_scope_refuses_other_sites() -> None:
    assert same_scope("https://other.example/", "https://shop.example/") is False


def test_same_scope_subdomains_only_when_allowed() -> None:
    assert same_scope("https://blog.shop.example/", "shop.example") is False
    assert same_scope("https://blog.shop.example/", "shop.example", allow_subdomains=True) is True
    assert same_scope("https://evilshop.example/", "shop.example", allow_subdomains=True) is False


def test_website_candidates_tries_https_before_http_and_www_twin() -> None:
    assert website_candidates("medixsignatureclinic.com") == [
        "https://medixsignatureclinic.com/",
        "http://medixsignatureclinic.com/",
        "https://www.medixsignatureclinic.com/",
        "http://www.medixsignatureclinic.com/",
    ]


def test_website_candidates_keeps_path_query_and_port() -> None:
    assert website_candidates("http://Cafe.Example:8080/menu?x=1") == [
        "https://cafe.example:8080/menu?x=1",
        "http://cafe.example:8080/menu?x=1",
        "https://www.cafe.example:8080/menu?x=1",
        "http://www.cafe.example:8080/menu?x=1",
    ]


def test_website_candidates_strips_www_for_the_second_host() -> None:
    assert website_candidates("www.a.com")[:2] == [
        "https://www.a.com/",
        "http://www.a.com/",
    ]
    assert "https://a.com/" in website_candidates("www.a.com")


def test_website_candidates_encodes_idn_host() -> None:
    assert website_candidates("bücher.de")[0] == "https://xn--bcher-kva.de/"


def test_website_candidates_skips_www_for_ip_and_single_label() -> None:
    assert website_candidates("192.168.1.10") == [
        "https://192.168.1.10/",
        "http://192.168.1.10/",
    ]
    assert website_candidates("localhost") == [
        "https://localhost/",
        "http://localhost/",
    ]


@pytest.mark.parametrize("raw", ["", "   ", "ftp://a.com", "mailto:x@a.com", "https://"])
def test_website_candidates_rejects_unusable_input(raw: str) -> None:
    with pytest.raises(ValueError):
        website_candidates(raw)
