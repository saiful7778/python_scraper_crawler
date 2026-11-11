"""Typed shapes of the dicts that pass between the scrape modules."""

from dataclasses import dataclass
from typing import Literal, NotRequired, TypedDict, TypeGuard, get_args

LeadKey = Literal[
    "input_id",
    "link",
    "title",
    "address",
    "open_hours",
    "popular_times",
    "website",
    "phone",
    "plus_code",
    "review_count",
    "review_rating",
    "reviews_per_rating",
    "review_star_1_count",
    "review_star_2_count",
    "review_star_3_count",
    "review_star_4_count",
    "review_star_5_count",
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
    "categories",
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
    address: str
    open_hours: str
    popular_times: str
    website: str
    phone: str
    plus_code: str
    review_count: str
    review_rating: str
    reviews_per_rating: str
    review_star_1_count: str
    review_star_2_count: str
    review_star_3_count: str
    review_star_4_count: str
    review_star_5_count: str
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
    categories: str


class SettingsData(TypedDict):
    """The settings stored in ``scrape_records.settings``.

    Only ``keywords``, ``lang`` and ``max_time`` are always present; the other
    keys may be missing in scrapes written by older versions.
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
    proxies: NotRequired[list[str]]
    crawl_max_pages: NotRequired[int]
    crawl_max_depth: NotRequired[int]


class ProgressData(TypedDict, total=False):
    """The progress of a scrape, saved so an interrupted scrape can resume."""

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


class ScrapeRecord(TypedDict):
    """A stored scrape as the engine works with it."""

    ID: str
    Name: str
    Date: str
    Status: str
    Data: SettingsData
    Error: str
    Progress: ProgressData


@dataclass(frozen=True)
class RunResult:
    """The outcome of one scrape run to its end."""

    scrape_id: str
    status: str
    note: str
    rows: list[LeadRow]
    blocked: bool


@dataclass
class ScrapeResult:
    """Rows that passed the filters, plus what happened while finding them."""

    rows: list[LeadRow]
    columns: list[str]
    finished: int
    skipped: int
    blocked: bool


