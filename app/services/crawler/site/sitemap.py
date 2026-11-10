from dataclasses import dataclass
from xml.etree import ElementTree


@dataclass(frozen=True)
class SitemapResult:
    urls: list[str]
    sitemaps: list[str]


def parse_sitemap(
    content: bytes,
    max_bytes: int = 2_000_000,
    max_urls: int = 10_000,
) -> SitemapResult:
    if len(content) > max_bytes:
        raise ValueError("sitemap response is too large")
    try:
        root = ElementTree.fromstring(content)
    except ElementTree.ParseError as exc:
        raise ValueError("invalid sitemap XML") from exc
    tag = root.tag.rsplit("}", 1)[-1].lower()
    values = []
    for node in root.iter():
        if node.tag.rsplit("}", 1)[-1].lower() == "loc" and node.text:
            value = node.text.strip()
            if value and value not in values:
                values.append(value)
            if len(values) >= max_urls:
                break
    return SitemapResult(
        values if tag == "urlset" else [],
        values if tag == "sitemapindex" else [],
    )


def sitemap_urls(body: bytes) -> tuple[list[str], list[str]]:
    result = parse_sitemap(body)
    return result.urls, result.sitemaps
