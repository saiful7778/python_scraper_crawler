"""Reads SEO signals (title, meta tags, headings, images, links) from one page."""

from urllib.parse import urlsplit

from app.utils.errors import describe
from app.utils.logger import get_logger

from ..document import Document, absolute
from ..findings import Finding, json_ld_items, schema_types

logger = get_logger(__name__)

NON_PAGE_SCHEMES = ("mailto:", "tel:", "javascript:", "sms:", "data:", "#")


def _meta(doc: Document, attr: str, name: str) -> str:
    for item in doc.elements:
        attrs = item["attrs"]
        if item["tag"] == "meta" and (attrs.get(attr) or "").lower() == name:
            return (attrs.get("content") or "").strip()
    return ""


def _host(url: str) -> str:
    host = urlsplit(url).netloc.lower()
    return host.removeprefix("www.")


def find_seo(doc: Document, page_url: str) -> list[Finding]:
    title = next((item["text"] for item in doc.elements if item["tag"] == "title"), "")
    h1 = next((value for tag, value in doc.heading_text if tag == "h1"), "")
    canonical = next(
        (
            item["attrs"]["href"]
            for item in doc.elements
            if item["tag"] == "link"
            and "canonical" in (item["attrs"].get("rel") or "").lower().split()
            and item["attrs"].get("href")
        ),
        "",
    )
    og_image = _meta(doc, "property", "og:image")
    values = {
        "title": title,
        "meta_description": _meta(doc, "name", "description"),
        "h1": h1,
        "canonical_url": absolute(page_url, canonical) if canonical else "",
        "robots_meta": _meta(doc, "name", "robots"),
        "og_title": _meta(doc, "property", "og:title"),
        "og_description": _meta(doc, "property", "og:description"),
        "og_image": absolute(page_url, og_image) if og_image else "",
        "twitter_card": _meta(doc, "name", "twitter:card"),
    }
    rows = [Finding("seo", key, value) for key, value in values.items() if value]

    for node in json_ld_items(doc):
        for name in schema_types(node):
            finding = Finding("seo", "structured_data_type", name)
            if finding not in rows:
                rows.append(finding)

    words = sum(len(part.split()) for part in doc.text)
    if words:
        rows.append(Finding("seo", "word_count", str(words)))

    images = [item for item in doc.elements if item["tag"] == "img"]
    if images:
        missing = sum(1 for item in images if "alt" not in item["attrs"])
        rows.append(Finding("seo", "image_count", str(len(images))))
        rows.append(Finding("seo", "image_missing_alt_count", str(missing)))

    site = _host(page_url)
    internal = external = 0
    for item in doc.elements:
        href = (item["attrs"].get("href") or "").strip()
        if item["tag"] != "a" or not href or href.lower().startswith(NON_PAGE_SCHEMES):
            continue
        try:
            target = absolute(page_url, href)
            if urlsplit(target).scheme not in ("http", "https"):
                continue
            is_internal = _host(target) == site
        except ValueError as error:
            logger.debug(
                "skipped malformed link %r on %s: %s", href, page_url, describe(error)
            )
            continue
        if is_internal:
            internal += 1
        else:
            external += 1
    if internal or external:
        rows.append(Finding("seo", "internal_link_count", str(internal)))
        rows.append(Finding("seo", "external_link_count", str(external)))
    return rows
