from typing import cast
from uuid import UUID, uuid4

from tortoise import fields
from tortoise.models import Model

from ..extract_details import address_json
from .scrape_types import LeadRow, SettingsData

SCHEMA_VERSION = 12

ADDRESS_PART_NAMES = ("borough", "street", "city", "postal_code", "state", "country")


class ScrapeModel(Model):
    id = fields.UUIDField(primary_key=True, default=uuid4)
    name = fields.CharField(max_length=200)
    created_order = fields.IntField(index=True)
    status = fields.CharField(max_length=16, index=True)
    stage = fields.CharField(max_length=16, default="")
    error = fields.TextField(default="")
    keywords_done = fields.IntField(default=0)
    rows_saved = fields.IntField(default=0)
    leads = fields.IntField(default=0)
    percent = fields.IntField(default=0)
    failed_count = fields.IntField(default=0)
    last_error = fields.TextField(default="")
    blocked = fields.BooleanField(default=False)
    retries = fields.IntField(default=0)
    timed_out = fields.BooleanField(default=False)
    elapsed_seconds = fields.FloatField(default=0)
    reviews_done = fields.IntField(default=0)
    reviews_total = fields.IntField(default=0)
    resumes = fields.IntField(default=0)
    created_at = fields.DatetimeField(auto_now_add=True)
    updated_at = fields.DatetimeField(auto_now=True)

    settings_row: "ScrapeSettingsModel"
    keywords: fields.ReverseRelation["ScrapeKeywordModel"]
    proxies: fields.ReverseRelation["ScrapeProxyModel"]
    lead_rows: fields.ReverseRelation["ScrapeLeadModel"]

    class Meta(Model.Meta):
        table = "scrapes"


class ScrapeSettingsModel(Model):
    id = fields.IntField(primary_key=True, generated=True)
    scrape = fields.OneToOneField(
        "engine_scrapes.ScrapeModel", related_name="settings_row", on_delete=fields.CASCADE
    )
    language = fields.CharField(max_length=2)
    country = fields.CharField(max_length=2, default="")
    latitude = fields.FloatField(null=True)
    longitude = fields.FloatField(null=True)
    zoom = fields.IntField()
    scroll_depth = fields.IntField()
    max_seconds = fields.IntField()
    fast_mode = fields.BooleanField(default=False)
    collect_emails = fields.BooleanField(default=False)
    collect_socials = fields.BooleanField(default=False)
    limit = fields.IntField(null=True)
    crawl_max_pages = fields.IntField(default=100)
    crawl_max_depth = fields.IntField(default=3)
    crawl_store_html = fields.BooleanField(default=True)

    class Meta(Model.Meta):
        table = "scrape_settings"


class ScrapeKeywordModel(Model):
    id = fields.IntField(primary_key=True, generated=True)
    scrape = fields.ForeignKeyField(
        "engine_scrapes.ScrapeModel", related_name="keywords", on_delete=fields.CASCADE
    )
    position = fields.IntField()
    term = fields.CharField(max_length=500)

    class Meta(Model.Meta):
        table = "scrape_keywords"
        unique_together = (("scrape", "position"),)


class ScrapeProxyModel(Model):
    id = fields.IntField(primary_key=True, generated=True)
    scrape = fields.ForeignKeyField(
        "engine_scrapes.ScrapeModel", related_name="proxies", on_delete=fields.CASCADE
    )
    position = fields.IntField()
    url = fields.TextField()

    class Meta(Model.Meta):
        table = "scrape_proxies"
        unique_together = (("scrape", "position"),)


class ScrapeLeadModel(Model):
    id = fields.IntField(primary_key=True, generated=True)
    scrape = fields.ForeignKeyField(
        "engine_scrapes.ScrapeModel", related_name="lead_rows", on_delete=fields.CASCADE
    )
    scrape_id: UUID
    position = fields.IntField()
    input_id = fields.TextField(default="")
    link = fields.TextField(default="")
    title = fields.TextField(default="")
    category = fields.TextField(default="")
    address = fields.TextField(default="")
    open_hours = fields.TextField(default="")
    popular_times = fields.TextField(default="")
    website = fields.TextField(default="")
    phone = fields.TextField(default="")
    plus_code = fields.TextField(default="")
    review_count = fields.TextField(default="")
    review_rating = fields.TextField(default="")
    reviews_per_rating = fields.TextField(default="")
    review_star_1_count = fields.TextField(default="")
    review_star_2_count = fields.TextField(default="")
    review_star_3_count = fields.TextField(default="")
    review_star_4_count = fields.TextField(default="")
    review_star_5_count = fields.TextField(default="")
    latitude = fields.FloatField(null=True)
    longitude = fields.FloatField(null=True)
    cid = fields.TextField(default="")
    status = fields.TextField(default="")
    descriptions = fields.TextField(default="")
    reviews_link = fields.TextField(default="")
    thumbnail = fields.TextField(default="")
    timezone = fields.TextField(default="")
    price_range = fields.TextField(default="")
    data_id = fields.TextField(default="")
    street_view_url = fields.TextField(default="")
    place_id = fields.TextField(default="")
    images = fields.TextField(default="")
    reservations = fields.TextField(default="")
    order_online = fields.TextField(default="")
    menu = fields.TextField(default="")
    owner = fields.TextField(default="")
    credit_cards_accepted = fields.TextField(default="")
    about = fields.TextField(default="")
    user_reviews = fields.TextField(default="")
    user_reviews_extended = fields.TextField(default="")
    emails = fields.TextField(default="")
    categories = fields.TextField(default="")

    class Meta(Model.Meta):
        table = "scrape_leads"
        unique_together = (("scrape", "position"),)


class ScrapeAddressModel(Model):
    """The address of a scraped lead, split into parts."""

    id = fields.IntField(primary_key=True, generated=True)
    lead = fields.OneToOneField(
        "engine_scrapes.ScrapeLeadModel", related_name="detailed_address", on_delete=fields.CASCADE
    )
    complete_address = fields.TextField(default="")
    borough = fields.TextField(default="")
    street = fields.TextField(default="")
    city = fields.TextField(default="")
    state = fields.TextField(default="")
    postal_code = fields.TextField(default="")
    country = fields.TextField(default="")
    latitude = fields.FloatField(null=True)
    longitude = fields.FloatField(null=True)
    plus_code = fields.TextField(default="")

    class Meta(Model.Meta):
        table = "scrape_addresses"


def lead_of(record: ScrapeLeadModel, address: ScrapeAddressModel | None) -> LeadRow:
    """Rebuild the ``LeadRow`` stored in this record; website fields come from ``CrawlWebsiteModel``."""
    row: dict[str, object] = {
        name: getattr(record, name)
        for name in LeadRow.__required_keys__
        if name != "complete_address"
    }
    row["complete_address"] = address_json(
        {name: getattr(address, name) for name in ADDRESS_PART_NAMES}
        if address
        else None
    )
    return cast(LeadRow, row)


def settings_columns(data: SettingsData) -> dict[str, object]:
    """Column values of a ``ScrapeSettingsModel`` from the stored settings dict."""
    return {
        "language": data["lang"],
        "country": data.get("country", ""),
        "latitude": data.get("lat"),
        "longitude": data.get("lon"),
        "zoom": data.get("zoom", 15),
        "scroll_depth": data.get("depth", 1),
        "max_seconds": data["max_time"],
        "fast_mode": data.get("fast_mode", False),
        "collect_emails": data.get("email", False),
        "collect_socials": data.get("socials", False),
        "limit": data.get("limit"),
        "crawl_max_pages": data.get("crawl_max_pages", 100),
        "crawl_max_depth": data.get("crawl_max_depth", 3),
        "crawl_store_html": data.get("crawl_store_html", True),
    }


def settings_of(scrape: ScrapeModel) -> SettingsData:
    """Rebuild the stored settings dict; needs ``settings_row``, ``keywords`` and ``proxies`` prefetched."""
    row = scrape.settings_row
    return cast(
        SettingsData,
        {
            "keywords": [
                k.term for k in sorted(scrape.keywords, key=lambda k: k.position)
            ],
            "lang": row.language,
            "country": row.country,
            "lat": row.latitude,
            "lon": row.longitude,
            "zoom": row.zoom,
            "depth": row.scroll_depth,
            "max_time": row.max_seconds,
            "fast_mode": row.fast_mode,
            "email": row.collect_emails,
            "socials": row.collect_socials,
            "proxies": [p.url for p in sorted(scrape.proxies, key=lambda p: p.position)],
            "limit": row.limit,
            "crawl_max_pages": row.crawl_max_pages,
            "crawl_max_depth": row.crawl_max_depth,
            "crawl_store_html": row.crawl_store_html,
        },
    )
