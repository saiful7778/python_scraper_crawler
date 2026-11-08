"""Typed shapes of the dicts that pass between the job modules."""

from typing import Literal, NotRequired, TypedDict, TypeGuard, get_args

SocialKey = Literal["instagram", "facebook", "linkedin", "tiktok", "youtube", "x"]

LeadKey = Literal[
    "input_id",
    "link",
    "title",
    "category",
    "address",
    "open_hours",
    "popular_times",
    "website",
    "phone",
    "plus_code",
    "review_count",
    "review_rating",
    "reviews_per_rating",
    "latitude",
    "longitude",
    "cid",
    "status",
    "descriptions",
    "reviews_link",
    "thumbnail",
    "timezone",
    "price_range",
    "data_id",
    "street_view_url",
    "place_id",
    "images",
    "reservations",
    "order_online",
    "menu",
    "owner",
    "complete_address",
    "credit_cards_accepted",
    "about",
    "user_reviews",
    "user_reviews_extended",
    "emails",
    "categories",
    "instagram",
    "facebook",
    "linkedin",
    "tiktok",
    "youtube",
    "x",
    "website_fetch_status",
]

_LEAD_KEYS = frozenset(get_args(LeadKey))


def is_lead_key(key: str) -> TypeGuard[LeadKey]:
    """True when ``key`` is a column of an output row."""
    return key in _LEAD_KEYS


class LeadRow(TypedDict):
    """One output row. Every column is text except the coordinates, which are ``None`` when blank."""

    input_id: str
    link: str
    title: str
    category: str
    address: str
    open_hours: str
    popular_times: str
    website: str
    phone: str
    plus_code: str
    review_count: str
    review_rating: str
    reviews_per_rating: str
    latitude: float | None
    longitude: float | None
    cid: str
    status: str
    descriptions: str
    reviews_link: str
    thumbnail: str
    timezone: str
    price_range: str
    data_id: str
    street_view_url: str
    place_id: str
    images: str
    reservations: str
    order_online: str
    menu: str
    owner: str
    complete_address: str
    credit_cards_accepted: str
    about: str
    user_reviews: str
    user_reviews_extended: str
    emails: str
    categories: str
    instagram: NotRequired[str]
    facebook: NotRequired[str]
    linkedin: NotRequired[str]
    tiktok: NotRequired[str]
    youtube: NotRequired[str]
    x: NotRequired[str]
    website_fetch_status: NotRequired[str]


class SettingsData(TypedDict):
    """The settings stored in ``job_records.settings``.

    Only ``keywords``, ``lang`` and ``max_time`` are always present; the other
    keys may be missing in jobs written by older versions.
    """

    keywords: list[str]
    lang: str
    country: NotRequired[str]
    lat: NotRequired[float]
    lon: NotRequired[float]
    zoom: NotRequired[int]
    radius: NotRequired[int]
    depth: NotRequired[int]
    max_time: int
    fast_mode: NotRequired[bool]
    email: NotRequired[bool]
    socials: NotRequired[bool]
    browser_sites: NotRequired[bool]
    proxies: NotRequired[list[str]]


class ProgressData(TypedDict, total=False):
    """The progress of a job, saved so an interrupted job can resume."""

    stage: str
    done: int
    saved: int
    leads: int
    percent: int
    failed: int
    last_error: str
    blocked: bool
    retried: int
    timed_out: bool
    elapsed: float
    reviews_done: int
    reviews_total: int
    resumes: int


class JobRecord(TypedDict):
    """A stored job as the engine works with it."""

    ID: str
    Name: str
    Date: str
    Status: str
    Data: SettingsData
    Error: str
    Progress: ProgressData


class PublicProgress(TypedDict):
    """The progress summary shown to API and UI clients."""

    keywords_done: int
    keywords_total: int
    leads: int
    percent: int
    stage: str
    reviews_done: NotRequired[int]
    reviews_total: NotRequired[int]


class PublicJob(TypedDict):
    """A job as shown to API and UI clients."""

    ID: str
    Name: str
    Date: str
    Status: str
    Data: SettingsData
    Error: str
    Progress: PublicProgress
