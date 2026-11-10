"""Detects tracking pixels, the CMS and mobile setup of one fetched website page."""

import re

from ..document import Document
from ..findings import Finding, doc_markup

TECH_RE = {
    "fb_pixel": re.compile(
        r"connect\.facebook\.net/[^\"']{0,300}fbevents\.js|fbq\(",
        re.IGNORECASE,
    ),
    "google_ads_tag": re.compile(
        r"googleadservices\.com|AW-\d{6,}|googleads\.g\.doubleclick\.net",
        re.IGNORECASE,
    ),
    "analytics": re.compile(
        r"googletagmanager\.com/gtag|gtag\(|UA-\d{4,}|G-[A-Z0-9]{6,}",
        re.IGNORECASE,
    ),
    "bing_ads": re.compile(r"bat\.bing\.com|window\.uetq", re.IGNORECASE),
    "linkedin_insight": re.compile(
        r"snap\.licdn\.com|_linkedin_partner_id", re.IGNORECASE
    ),
    "tiktok_pixel": re.compile(r"analytics\.tiktok\.com|ttq\.load\(", re.IGNORECASE),
}
CMS_RE = [
    ("wordpress", re.compile(r"/wp-content/|/wp-includes/", re.IGNORECASE)),
    (
        "shopify",
        re.compile(r"cdn\.shopify\.com|shopify\.theme", re.IGNORECASE),
    ),
    (
        "wix",
        re.compile(r"static\.wixstatic\.com|_wixcss|wix\.com", re.IGNORECASE),
    ),
    (
        "squarespace",
        re.compile(r"static1\.squarespace|squarespace\.com", re.IGNORECASE),
    ),
    ("webflow", re.compile(r"webflow\.com", re.IGNORECASE)),
]
VIEWPORT_RE = re.compile(r"<meta[^>]{1,500}name=[\"']viewport[\"']", re.IGNORECASE)
TECH_FIELDS = list(TECH_RE) + ["cms", "mobile_friendly"]


def tech_fields(page: str) -> dict[str, str]:
    """Return the tech values of a page: ``yes`` per tracker found, the CMS name, and ``yes``/``no`` for mobile."""
    fields = {name: "yes" for name, pattern in TECH_RE.items() if pattern.search(page)}
    cms = next((name for name, pattern in CMS_RE if pattern.search(page)), "")
    if cms:
        fields["cms"] = cms
    fields["mobile_friendly"] = "yes" if VIEWPORT_RE.search(page) else "no"
    return fields


FRAMEWORK_RE = [
    (
        "React",
        re.compile(
            r"data-reactroot|data-reactid|react(?:-dom)?(?:\.production)?\.min\.js",
            re.IGNORECASE,
        ),
    ),
    ("Next.js", re.compile(r"/_next/|__NEXT_DATA__")),
    (
        "Vue",
        re.compile(
            r"data-v-[0-9a-f]{6,}|vue(?:\.runtime)?(?:\.global)?(?:\.min)?\.js|__VUE__",
            re.IGNORECASE,
        ),
    ),
    ("Angular", re.compile(r"\bng-version=|\bng-app\b|<app-root", re.IGNORECASE)),
    (
        "Laravel",
        re.compile(r"laravel_session|/vendor/laravel|XSRF-TOKEN", re.IGNORECASE),
    ),
]


def find_tech(doc: Document, page_url: str) -> list[Finding]:
    markup = doc_markup(doc)
    rows = [Finding("tech", key, value) for key, value in tech_fields(markup).items()]
    rows.extend(
        Finding("tech", "framework", name)
        for name, pattern in FRAMEWORK_RE
        if pattern.search(markup)
    )
    html_tag = next((item for item in doc.elements if item["tag"] == "html"), None)
    language = ((html_tag or {"attrs": {}})["attrs"].get("lang") or "").strip().lower()
    if language:
        rows.append(Finding("tech", "language", language))
    return rows
