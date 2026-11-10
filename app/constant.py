from typing import Final, Literal

SocialKey = Literal["instagram", "facebook", "linkedin", "tiktok", "youtube", "x"]

SOCIALS: Final[tuple[SocialKey, ...]] = (
    "instagram",
    "facebook",
    "linkedin",
    "tiktok",
    "youtube",
    "x",
)
