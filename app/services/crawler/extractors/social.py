import re

from ..document import Document
from ..findings import Finding, doc_markup

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


def find_social(doc: Document, page_url: str) -> list[Finding]:
    profiles = find_socials(doc_markup(doc))
    return [Finding("social", network, url) for network, url in profiles.items() if url]
