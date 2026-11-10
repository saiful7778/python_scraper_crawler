import re
import urllib.parse

from app.utils.url_utils import is_junk_domain

_EMAIL_PATTERN = re.compile(
    r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}"
)

MAX_EMAILS = 3
JUNK_EMAIL_SUFFIXES = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".css", ".js")


def find_emails(text: str) -> list[str]:
    found: list[str] = []
    for match in _EMAIL_PATTERN.findall(text):
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

    return found
