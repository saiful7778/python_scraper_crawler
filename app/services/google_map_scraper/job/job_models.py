from typing import cast
from uuid import uuid4

from tortoise import fields
from tortoise.models import Model

from .job_types import LeadRow, SettingsData

SCHEMA_VERSION = 4


class JobModel(Model):
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

    settings_row: "JobSettingsModel"
    keywords: fields.ReverseRelation["JobKeywordModel"]
    proxies: fields.ReverseRelation["JobProxyModel"]
    lead_rows: fields.ReverseRelation["JobLeadModel"]

    class Meta(Model.Meta):
        table = "jobs"


class JobSettingsModel(Model):
    id = fields.IntField(primary_key=True, generated=True)
    job = fields.OneToOneField(
        "engine_jobs.JobModel", related_name="settings_row", on_delete=fields.CASCADE
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
    use_browser_for_websites = fields.BooleanField(default=False)
    limit = fields.IntField(null=True)

    class Meta(Model.Meta):
        table = "job_settings"


class JobKeywordModel(Model):
    id = fields.IntField(primary_key=True, generated=True)
    job = fields.ForeignKeyField(
        "engine_jobs.JobModel", related_name="keywords", on_delete=fields.CASCADE
    )
    position = fields.IntField()
    term = fields.CharField(max_length=500)

    class Meta(Model.Meta):
        table = "job_keywords"
        unique_together = (("job", "position"),)


class JobProxyModel(Model):
    id = fields.IntField(primary_key=True, generated=True)
    job = fields.ForeignKeyField(
        "engine_jobs.JobModel", related_name="proxies", on_delete=fields.CASCADE
    )
    position = fields.IntField()
    url = fields.TextField()

    class Meta(Model.Meta):
        table = "job_proxies"
        unique_together = (("job", "position"),)


class JobLeadModel(Model):
    id = fields.IntField(primary_key=True, generated=True)
    job = fields.ForeignKeyField(
        "engine_jobs.JobModel", related_name="lead_rows", on_delete=fields.CASCADE
    )
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
    complete_address = fields.TextField(default="")
    credit_cards_accepted = fields.TextField(default="")
    about = fields.TextField(default="")
    user_reviews = fields.TextField(default="")
    user_reviews_extended = fields.TextField(default="")
    emails = fields.TextField(default="")
    categories = fields.TextField(default="")
    instagram = fields.TextField(null=True)
    facebook = fields.TextField(null=True)
    linkedin = fields.TextField(null=True)
    tiktok = fields.TextField(null=True)
    youtube = fields.TextField(null=True)
    x = fields.TextField(null=True)
    website_fetch_status = fields.TextField(null=True)

    class Meta(Model.Meta):
        table = "job_leads"
        unique_together = (("job", "position"),)


def lead_of(record: JobLeadModel) -> LeadRow:
    """Rebuild a ``LeadRow``; a NULL optional column is an absent key."""
    row: dict[str, object] = {
        name: getattr(record, name) for name in LeadRow.__required_keys__
    }
    for name in LeadRow.__optional_keys__:
        value = getattr(record, name)
        if value is not None:
            row[name] = value
    return cast(LeadRow, row)


def settings_columns(data: SettingsData) -> dict[str, object]:
    """Column values of a ``JobSettingsModel`` from the stored settings dict."""
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
        "use_browser_for_websites": data.get("browser_sites", False),
        "limit": data.get("limit"),
    }


def settings_of(job: JobModel) -> SettingsData:
    """Rebuild the stored settings dict; needs ``settings_row``, ``keywords`` and ``proxies`` prefetched."""
    row = job.settings_row
    return cast(
        SettingsData,
        {
            "keywords": [
                k.term for k in sorted(job.keywords, key=lambda k: k.position)
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
            "browser_sites": row.use_browser_for_websites,
            "proxies": [p.url for p in sorted(job.proxies, key=lambda p: p.position)],
            "limit": row.limit,
        },
    )
