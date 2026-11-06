import json
import math
import urllib.parse
from collections.abc import Sequence
from typing import Any, TypedDict, cast

from app.utils import normalize_unicode_text

Path = Sequence[int]


class ReviewRecord(TypedDict):
    """One review shown in the place panel."""

    Rating: int
    When: str
    Description: str


class PlaceRecord(TypedDict, total=False):
    """One place from a search result. A key is present only when the source has a value."""

    title: str
    category: str
    address: str
    website: str
    phone: str
    review_rating: float
    review_count: int
    reviews_link: str
    latitude: float
    longitude: float
    place_id: str
    data_id: str
    open_state: str | int | float
    thumbnail: str
    timezone: str
    price_range: str
    description: str
    categories: str
    address_lines: str
    open_hours: str
    images: str
    complete_address: str
    about: str
    credit_cards_accepted: str
    reservations: str
    order_online: str
    menu: str
    cid: str


class PanelRecord(TypedDict, total=False):
    """The fields of a place panel. A key is present only when the source has a value."""

    review_count: int
    reviews_link: str
    open_hours: str
    complete_address: str
    about: str
    credit_cards_accepted: str
    reservations: str
    order_online: str
    menu: str
    reviews_per_rating: str
    user_reviews: str


IMAGES_MAX, IMAGE_URL_MAX = (
    20,
    1000,
)  # keeps the cell far below csv's 128 KB field limit

# Index path of each field inside a place entity. Checked on live batches in the spike
# (2026-10-31): title, category, address, rating, lat/lon, place_id, data_id, timezone, thumbnail
# filled on 60/60 entities, website 55/60, phone 46/60 (listings without one).
# review_count (4, 8), reviews_link (4, 3, 0) and the week of opening hours come only with Google's
# full view: a browser whose user agent says HeadlessChrome gets its "limited view", which leaves them
# empty and lists today's hours only (browser.py sends Chrome/ instead, see there). Full view
# (2026-10-31): count on 20/20 entities, 7 days of hours on 19/20. (37, 1) is the photo count.
FIELD_PATHS = {
    "title": (11,),
    "category": (13, 0),
    "address": (18,),
    "website": (7, 0),
    "phone": (178, 0, 0),
    "review_rating": (4, 7),
    "review_count": (4, 8),
    "reviews_link": (4, 3, 0),
    "latitude": (9, 2),
    "longitude": (9, 3),
    "place_id": (78,),
    "data_id": (10,),
    "open_state": (203, 1, 4, 0),
    "thumbnail": (72, 0, 0, 6, 0),
    "timezone": (30,),
    "price_range": (4, 2),
    "description": (32, 1, 1),
}
MENU_PATH = (38,)  # [link, source]
HOURS_PATH = (
    203,
    0,
)  # [[day name, 1-7 (Monday-Sunday), date, [[text, ...], ...], ...], ...], from today on
ABOUT_PATH = (
    100,
    1,
)  # [[group id, group name, [[?, option name, [?, [[1 if it applies, ...]], ...]], ...]], ...]
ADDRESS_PATH = (183, 1)  # [borough, street, ?, city, postal code, state, country]
ADDRESS_PARTS = {
    "borough": 0,
    "street": 1,
    "city": 3,
    "postal_code": 4,
    "state": 5,
    "country": 6,
}
REVIEWS_PATH = (175, 9, 0, 0)  # place panel: the reviews it shows first (about 5)
LISTS = {"categories": (13,), "address_lines": (2,)}
OLC = "23456789CFGHJMPQRVWX"  # Open Location Code (Plus Code) digits
# What the reviews pass takes from a place's panel for a row that lacks it (a DOM-fallback row).
PANEL_FIELDS = (
    "review_count",
    "reviews_link",
    "open_hours",
    "complete_address",
    "about",
    "credit_cards_accepted",
    "reservations",
    "order_online",
    "menu",
)
HISTOGRAM_PATH = (175, 3)  # place panel: [one-star count, ..., five-star count]
# (75, 0): the listing's action groups, [type, ?, items, ...]. Type 1 is "Reserve a table" (the same
# links as RESERVATIONS), 3 the order links of delivery and ordering sites, 4 Google's own order flow
# (no items). The old engine also read the first group, which lists reservations as order links.
ACTIONS_PATH, ORDER_GROUP_PATH = (75, 0), 3
RESERVATIONS_PATH = (46,)  # [[link, source, ...], ...]
ORDER_ITEM_PATH = (
    (1, 2, 0),
    (0, 0),
)  # link, source of each [[source, ...], [?, ?, [link]], ...] item
ENTRIES_PATH = 64  # position of the result list inside a batch

# Paths applied directly inside the functions below. Each one is relative to the node
# named in its comment, so a change to Google's layout only needs an edit here.

# Gallery (format_gallery). Search results and the place panel use different layouts.
GALLERY_SEARCH_CARDS_PATH = (171, 0)  # search results: list of cards
GALLERY_SEARCH_PHOTO_PATH = (3, 0, 6, 0)  # photo URL inside a search card
GALLERY_SEARCH_TITLE_PATH = (2,)  # photo title inside a search card
GALLERY_PANEL_PHOTOS_PATH = (37, 0)  # place panel: list of photos
GALLERY_HEADER_PHOTOS_PATH = (72, 0)  # header photos: list of photos
GALLERY_ITEM_URL_PATH = (6, 0)  # photo URL inside a panel or header photo

# Opening hours (format_weekly_hours). Relative to one day of HOURS_PATH.
DAY_SPANS_PATH = (3,)  # [[text, ...], ...]: the time ranges of the day

# Attributes (extract_attributes_and_cards). Relative to a group of ABOUT_PATH.
ABOUT_GROUP_ID_PATH = (0,)  # group id, e.g. "payments"
ABOUT_GROUP_NAME_PATH = (1,)  # group display name
ABOUT_GROUP_OPTIONS_PATH = (2,)  # list of options in the group
ABOUT_OPTION_NAME_PATH = (1,)  # option display name
ABOUT_OPTION_APPLIES_PATH = (2, 1, 0, 0)  # 1 when the option applies
ABOUT_OPTION_VALUES_PATH = (2, 4, 1, 0, 0)  # list of value entries of the option
ABOUT_VALUE_TEXT_PATH = (2,)  # value text, checked first
ABOUT_VALUE_FALLBACK_TEXT_PATH = (3,)  # value text, used when the first is empty

# Action groups (build_place_record). Relative to an item of ACTIONS_PATH.
ACTION_GROUP_TYPE_PATH = (0,)  # group type, compared with ORDER_GROUP_PATH
ACTION_GROUP_ITEMS_PATH = (2,)  # list of items in the group

# Reservation items (format_sourced_links). Relative to an item of RESERVATIONS_PATH.
RESERVATION_LINK_PATH = (0,)  # link
RESERVATION_SOURCE_PATH = (1,)  # source name

# Place panel (parse_place_panel). Relative to the panel body or one review.
PANEL_ENTITY_PATH = (6,)  # place entity inside the panel response
REVIEW_WHEN_PATH = (0, 1, 6)  # relative time text of a review
REVIEW_RATING_PATH = (0, 2, 0, 0)  # star rating of a review
REVIEW_TEXT_PATH = (0, 2, 15, 0, 0)  # text of a review

# Result list (parse_place_list). Relative to one entry of ENTRIES_PATH.
ENTRY_ENTITY_PATH = (1,)  # place entity inside the entry


def get_at_path(node: Any, path: Path) -> Any:
    """Return the value at path inside node, or None if a step is missing or not a list."""
    for i in path:
        if not isinstance(node, list) or i >= len(node) or node[i] is None:
            return None
        node = node[i]
    return node


def get_list_at_path(node: Any, path: Path) -> list[Any]:
    """Return the list at path inside node, or an empty list if the value there is not a list."""
    v = get_at_path(node, path)
    return v if isinstance(v, list) else []


def is_safe_public_url(url: Any, schemes: tuple[str, ...] = ("https",)) -> bool:
    """Return True if url is a short string with an allowed scheme, a host, and no credentials or whitespace."""
    if (
        not isinstance(url, str)
        or len(url) > IMAGE_URL_MAX
        or any(c.isspace() or ord(c) < 32 for c in url)
    ):
        return False
    try:
        p = urllib.parse.urlsplit(url)
        return (
            p.scheme in schemes and bool(p.hostname) and not (p.username or p.password)
        )
    except ValueError:
        return False


def format_weekly_hours(days: Any) -> str:
    """Return the opening hours as JSON keyed by day name, Monday to Sunday, or "" unless all seven days are present."""
    if not isinstance(days, list):
        return ""
    week: dict[int, tuple[str, list[str]]] = {}
    for d in days:
        if not (
            isinstance(d, list)
            and len(d) > 3
            and isinstance(d[0], str)
            and isinstance(d[1], int)
        ):
            continue
        spans = [
            normalize_unicode_text(x[0])
            for x in get_list_at_path(d, DAY_SPANS_PATH)
            if isinstance(x, list) and x and normalize_unicode_text(x[0])
        ]
        week[d[1]] = (d[0], spans)
    if len(week) < 7:
        return ""
    return json.dumps(
        {name: spans for _, (name, spans) in sorted(week.items())},
        ensure_ascii=False,
    )


def format_gallery(entity: Any) -> str:
    """Return up to IMAGES_MAX unique, safe photos as JSON, or "" if there are none."""
    found: list[dict[str, str]] = []
    seen: set[str] = set()
    # Search cards and panels use different gallery layouts. Never include reviewer profiles.
    groups: list[tuple[Any, Path, Path]] = [
        (
            get_at_path(entity, GALLERY_SEARCH_CARDS_PATH),
            GALLERY_SEARCH_PHOTO_PATH,
            GALLERY_SEARCH_TITLE_PATH,
        ),
        (
            get_at_path(entity, GALLERY_PANEL_PHOTOS_PATH),
            GALLERY_ITEM_URL_PATH,
            (),
        ),
        (
            get_at_path(entity, GALLERY_HEADER_PHOTOS_PATH),
            GALLERY_ITEM_URL_PATH,
            (),
        ),
    ]
    for items, path, title_path in groups:
        if not isinstance(items, list):
            continue
        for item in items:
            url = get_at_path(item, path)
            if (
                not is_safe_public_url(url) or url in seen
            ):  # validate first: a list here is not hashable
                continue
            seen.add(url)
            title = (
                normalize_unicode_text(get_at_path(item, title_path))
                if title_path
                else ""
            )
            found.append({"title": title, "image": url})
            if len(found) == IMAGES_MAX:
                return json.dumps(found, ensure_ascii=False)
    return json.dumps(found, ensure_ascii=False) if found else ""


def format_address(entity: Any) -> str:
    """Return the address parts as JSON, or "" if every part is empty."""
    parts = get_at_path(entity, ADDRESS_PATH)
    if not isinstance(parts, list):
        return ""
    out = {
        k: normalize_unicode_text(parts[i]) if i < len(parts) else ""
        for k, i in ADDRESS_PARTS.items()
    }
    return json.dumps(out, ensure_ascii=False) if any(out.values()) else ""


def extract_attributes_and_cards(entity: Any) -> tuple[str, str]:
    """Return the attribute groups as JSON and the accepted credit cards as a comma-separated string."""
    groups: list[dict[str, Any]] = []
    cards: list[str] = []
    for g in get_list_at_path(entity, ABOUT_PATH):
        if not isinstance(g, list):
            continue
        gid = normalize_unicode_text(get_at_path(g, ABOUT_GROUP_ID_PATH))
        opts: list[dict[str, Any]] = []
        for o in get_list_at_path(g, ABOUT_GROUP_OPTIONS_PATH):
            name = normalize_unicode_text(get_at_path(o, ABOUT_OPTION_NAME_PATH))
            if not name:
                continue
            values = [
                v
                for v in (
                    normalize_unicode_text(get_at_path(x, ABOUT_VALUE_TEXT_PATH))
                    or normalize_unicode_text(
                        get_at_path(x, ABOUT_VALUE_FALLBACK_TEXT_PATH)
                    )
                    for x in get_list_at_path(o, ABOUT_OPTION_VALUES_PATH)
                )
                if v
            ]
            on = get_at_path(o, ABOUT_OPTION_APPLIES_PATH) == 1
            same = next((x for x in opts if x["name"] == name), None)
            if same:  # one option listed twice: it applies if either says so
                same["enabled"] = same["enabled"] or on
                values = [v for v in values if v not in same.get("values", [])]
                if values:
                    same["values"] = same.get("values", []) + values
            else:
                opts.append(
                    {
                        "name": name,
                        "enabled": on,
                        **({"values": values} if values else {}),
                    }
                )
            if gid == "payments" and name == "Credit cards":
                cards += [v for v in values if v not in cards]
        if gid or opts:
            groups.append(
                {
                    "id": gid,
                    "name": normalize_unicode_text(
                        get_at_path(g, ABOUT_GROUP_NAME_PATH)
                    ),
                    "options": opts,
                }
            )
    return (json.dumps(groups, ensure_ascii=False) if groups else ""), ", ".join(cards)


def format_sourced_links(nodes: Any, link: Path, source: Path) -> str:
    """Return the unique, safe links with their source names as JSON, or "" if there are none."""
    out: list[dict[str, str]] = []
    for it in nodes if isinstance(nodes, list) else []:
        url, src = (
            get_at_path(it, link),
            normalize_unicode_text(get_at_path(it, source)),
        )
        if (
            src
            and is_safe_public_url(url, ("https", "http"))
            and all(x["link"] != url for x in out)
        ):
            out.append({"link": url, "source": src})
    return json.dumps(out, ensure_ascii=False) if out else ""


def format_menu_link(entity: Any) -> str:
    """Return the menu link and its source as JSON, or "" if the link is not safe."""
    url = get_at_path(entity, MENU_PATH + (0,))
    src = normalize_unicode_text(get_at_path(entity, MENU_PATH + (1,)))
    return (
        json.dumps({"link": url, "source": src}, ensure_ascii=False)
        if is_safe_public_url(url, ("https", "http"))
        else ""
    )


def encode_plus_code(lat: Any, lon: Any) -> str:
    """Return the Open Location Code (Plus Code) for a coordinate, or "" if it is not a finite number."""
    try:
        lat, lon = float(lat), float(lon)
    except (TypeError, ValueError):
        return ""
    if not (math.isfinite(lat) and math.isfinite(lon)):
        return ""
    y = min(
        math.floor(round((min(max(lat, -90.0), 90.0) + 90) * 8000, 6)),
        180 * 8000 - 1,
    )  # 1/8000° cells
    x = math.floor(round(((lon + 180) % 360) * 8000, 6)) % (360 * 8000)
    code = ""
    for _ in range(5):  # base 20, least significant pair first
        code = OLC[y % 20] + OLC[x % 20] + code
        y, x = y // 20, x // 20
    return code[:8] + "+" + code[8:]


def parse_place_panel(body: str, limit: int = 5) -> PanelRecord:
    """Parse a place panel response into its fields and up to limit reviews, or {} if the body is invalid."""
    if body.startswith(")]}'"):
        body = body.split("\n", 1)[-1]
    try:
        entity = get_at_path(json.loads(body), PANEL_ENTITY_PATH)
    except ValueError:
        return {}
    if not isinstance(entity, list):
        return {}
    out: dict[str, Any] = {
        k: v for k, v in build_place_record(entity).items() if k in PANEL_FIELDS
    }
    hist = get_at_path(entity, HISTOGRAM_PATH)
    if (
        isinstance(hist, list)
        and len(hist) == 5
        and all(isinstance(n, int) and not isinstance(n, bool) for n in hist)
    ):
        out["reviews_per_rating"] = json.dumps(
            {str(i): n for i, n in enumerate(hist, 1)}
        )
    reviews: list[ReviewRecord] = []
    for r in get_list_at_path(entity, REVIEWS_PATH):
        rating = get_at_path(r, REVIEW_RATING_PATH)
        text = normalize_unicode_text(get_at_path(r, REVIEW_TEXT_PATH))
        if isinstance(rating, int) and not isinstance(rating, bool):
            reviews.append(
                {
                    "Rating": rating,
                    "When": normalize_unicode_text(get_at_path(r, REVIEW_WHEN_PATH)),
                    "Description": text,
                }
            )
        if len(reviews) >= limit:
            break
    if reviews:
        out["user_reviews"] = json.dumps(reviews, ensure_ascii=False)
    return cast(PanelRecord, out)


def build_place_record(entity: Any) -> PlaceRecord:
    """Build a place record from one place entity, leaving out empty fields."""
    out: dict[str, Any] = {}
    for name, path in FIELD_PATHS.items():
        v = get_at_path(entity, path)
        if isinstance(v, (str, int, float)) and not isinstance(v, bool) and v != "":
            out[name] = v
    for name, path in LISTS.items():
        v = get_at_path(entity, path)
        if isinstance(v, list):
            items = [x for x in v if isinstance(x, str) and x]
            if items:
                out[name] = " | ".join(items)
    hours = format_weekly_hours(get_at_path(entity, HOURS_PATH))
    if hours:
        out["open_hours"] = hours
    photos = format_gallery(entity)
    if photos:
        out["images"] = photos
    groups, cards = extract_attributes_and_cards(entity)
    orders = next(
        (
            get_at_path(g, ACTION_GROUP_ITEMS_PATH)
            for g in get_list_at_path(entity, ACTIONS_PATH)
            if get_at_path(g, ACTION_GROUP_TYPE_PATH) == ORDER_GROUP_PATH
        ),
        None,
    )
    found: dict[str, Any] = {
        "complete_address": format_address(entity),
        "about": groups,
        "credit_cards_accepted": cards,
        "reservations": format_sourced_links(
            get_at_path(entity, RESERVATIONS_PATH),
            RESERVATION_LINK_PATH,
            RESERVATION_SOURCE_PATH,
        ),
        "order_online": format_sourced_links(orders, *ORDER_ITEM_PATH),
        "menu": format_menu_link(entity),
    }
    out.update({k: v for k, v in found.items() if v})
    if isinstance(out.get("data_id"), str) and ":" in out["data_id"]:
        try:
            out["cid"] = str(int(out["data_id"].split(":")[1], 16))
        except ValueError:
            pass
    return cast(PlaceRecord, out)


def parse_place_list(body: str) -> list[PlaceRecord]:
    """Parse a search response into place records, skipping entries without a title."""
    if body.startswith(")]}'"):
        body = body.split("\n", 1)[-1]
    try:
        data = json.loads(body)
    except ValueError:
        return []
    entries_PATH = get_at_path(data, (ENTRIES_PATH,))
    if not isinstance(entries_PATH, list):
        return []
    out: list[PlaceRecord] = []
    for e in entries_PATH:
        entity = get_at_path(e, ENTRY_ENTITY_PATH)
        if isinstance(entity, list):
            try:
                row = build_place_record(entity)
            except (
                TypeError,
                ValueError,
                IndexError,
                AttributeError,
            ):  # one odd entity, not the whole batch
                continue
            if row.get("title"):
                out.append(row)
    return out
