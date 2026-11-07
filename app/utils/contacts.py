"""Pure helpers that find contact details in the text of a web page."""

import html
import re
import urllib.parse

from .url_utils import is_junk_domain

MAX_EMAILS = 3
JUNK_EMAIL_SUFFIXES = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".css", ".js")

_EMAIL_PATTERN = re.compile(
    r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}"
)

SOCIAL_PATTERNS: dict[str, re.Pattern[str]] = {
    "instagram": re.compile(
        r"https?://(?:www\.)?instagram\.com/([A-Za-z0-9._]{2,30})/?"
    ),
    "facebook": re.compile(
        r"https?://(?:www\.|m\.)?facebook\.com/([A-Za-z0-9.]{1,80})/?"
    ),
    "linkedin": re.compile(
        r"https?://(?:[a-z]{2,3}\.)?linkedin\.com/(?:company|in)/([A-Za-z0-9_-]{2,100})/?"
    ),
    "tiktok": re.compile(r"https?://(?:www\.)?tiktok\.com/@([A-Za-z0-9._]{2,30})/?"),
    "youtube": re.compile(
        r"https?://(?:www\.)?youtube\.com/(?:@|c/|channel/|user/)([A-Za-z0-9._-]{2,100})/?"
    ),
    "x": re.compile(r"https?://(?:www\.)?(?:twitter|x)\.com/([A-Za-z0-9_]{1,15})/?"),
}

# Share buttons and generic pages that match a social pattern but are not profiles.
NON_PROFILE_HANDLES = frozenset(
    {
        "sharer",
        "share",
        "intent",
        "home",
        "p",
        "watch",
        "reel",
        "dialog",
        "plugins",
        "tr",
        "pages",
        "explore",
        "login",
    }
)


def find_emails(text: str, site: str = "") -> list[str]:
    """Return up to three distinct email addresses found in ``text``.

    Addresses on the business's own ``site`` domain come first. Image file
    names, placeholder domains and percent-encoded junk are dropped.
    """
    found: list[str] = []
    for match in _EMAIL_PATTERN.findall(html.unescape(text or "")):
        email = urllib.parse.unquote(match).lower()
        domain = email.rpartition("@")[2]
        if (
            not _EMAIL_PATTERN.fullmatch(email)
            or email.endswith(JUNK_EMAIL_SUFFIXES)
            or is_junk_domain(domain)
            or email in found
        ):
            continue
        found.append(email)
    own = [e for e in found if site and e.endswith(("@" + site, "." + site))]
    return (own + [e for e in found if e not in own])[:MAX_EMAILS]


def find_socials(text: str) -> dict[str, str]:
    """Return the first profile URL found for each social network ("" if none)."""
    found = dict.fromkeys(SOCIAL_PATTERNS, "")
    for network, pattern in SOCIAL_PATTERNS.items():
        for match in pattern.finditer(text or ""):
            handle = match.group(1).lower()
            if handle in NON_PROFILE_HANDLES or (
                network == "facebook" and (handle.isdigit() or len(handle) < 3)
            ):
                continue
            found[network] = match.group(0).rstrip("\"'/").replace("\\", "")
            break
    return found
