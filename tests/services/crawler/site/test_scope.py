import pytest

from app.services.crawler.site.robots import RobotTxtPolicy
from app.services.crawler.site.scope import (
    is_in_scope,
    normalize,
    origin_of,
    registrable_domain,
)


@pytest.mark.parametrize(
    "raw, origin",
    [
        ("bakery.de", "https://bakery.de"),
        ("http://bakery.de/", "http://bakery.de"),
        ("https://www.bakery.de/shop?x=1", "https://www.bakery.de"),
    ],
)
def test_origin_and_domain_from_messy_urls(raw, origin):
    assert origin_of(raw) == origin


def test_registrable_domain_handles_subdomains_and_two_part_suffixes():
    assert registrable_domain("www.shop.bakery.de") == "bakery.de"
    assert registrable_domain("www.bakery.co.uk") == "bakery.co.uk"
    assert registrable_domain("bakery.de") == "bakery.de"
    assert registrable_domain("localhost") == "localhost"


def test_out_of_scope_hosts_are_rejected():
    assert is_in_scope("https://shop.bakery.de/a", "bakery.de")
    assert is_in_scope("https://bakery.de/", "bakery.de")
    assert not is_in_scope("https://notbakery.de/", "bakery.de")
    assert not is_in_scope("https://facebook.com/bakery", "bakery.de")


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("https://Bakery.DE/", "https://bakery.de/"),
        ("https://bakery.de/shop/", "https://bakery.de/shop"),
        ("https://bakery.de/shop#top", "https://bakery.de/shop"),
        ("https://bakery.de/shop/?a=1", "https://bakery.de/shop?a=1"),
    ],
)
def test_normalize_lowercases_host_and_drops_fragment_and_trailing_slash(raw, expected):
    assert normalize(raw) == expected


def test_robots_disallow_blocks_matching_path():
    policy = RobotTxtPolicy.from_text("User-agent: *\nDisallow: /private")
    assert not policy.is_allowed("https://a.de/private/x")
    assert policy.is_allowed("https://a.de/public")
