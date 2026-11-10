from uuid import UUID, uuid4

from tortoise import fields
from tortoise.models import Model

from app.constant import SOCIALS

WEBSITE_TECH_FIELDS: tuple[str, ...] = (
    "fb_pixel",
    "google_ads_tag",
    "analytics",
    "bing_ads",
    "linkedin_insight",
    "tiktok_pixel",
    "cms",
    "mobile_friendly",
)
WEBSITE_FIELDS: tuple[str, ...] = (*SOCIALS, *WEBSITE_TECH_FIELDS)


class CrawlWebsiteModel(Model):
    id = fields.UUIDField(primary_key=True, default=uuid4)
    scrape_id = fields.CharField(max_length=36)
    lead_position = fields.IntField()
    scrape_lead = fields.ForeignKeyField(
        "engine_scrapes.ScrapeLeadModel",
        related_name="crawl_websites",
        null=True,
        on_delete=fields.SET_NULL,
    )
    crawl_status = fields.CharField(max_length=16, default="")
    copied = fields.BooleanField(default=False)
    host = fields.CharField(max_length=255, default="")
    registrable_domain = fields.CharField(max_length=255, default="", db_index=True)
    origin = fields.CharField(max_length=2048, default="")
    status = fields.CharField(max_length=24, default="")
    error_code = fields.CharField(max_length=64, null=True)
    error_message = fields.TextField(null=True)
    created_at = fields.DatetimeField(auto_now_add=True)
    finished_at = fields.DatetimeField(null=True)
    fb_pixel = fields.TextField(null=True)
    google_ads_tag = fields.TextField(null=True)
    analytics = fields.TextField(null=True)
    bing_ads = fields.TextField(null=True)
    linkedin_insight = fields.TextField(null=True)
    tiktok_pixel = fields.TextField(null=True)
    cms = fields.TextField(null=True)
    mobile_friendly = fields.TextField(null=True)

    scrape_lead_id: int | None
    runs: fields.ReverseRelation["CrawlRunModel"]
    findings: fields.ReverseRelation["CrawlFindingModel"]
    socials: fields.ReverseRelation["CrawlSocialModel"]

    class Meta(Model.Meta):
        table = "crawl_websites"
        unique_together = (("scrape_id", "lead_position"),)


class CrawlSocialModel(Model):
    id = fields.IntField(primary_key=True, generated=True)
    website = fields.ForeignKeyField(
        "engine_crawler.CrawlWebsiteModel",
        related_name="socials",
        on_delete=fields.CASCADE,
    )
    website_id: UUID
    platform = fields.CharField(max_length=16)
    value = fields.TextField()

    class Meta(Model.Meta):
        table = "crawl_socials"
        unique_together = (("website", "platform", "value"),)


class CrawlRunModel(Model):
    id = fields.UUIDField(primary_key=True, default=uuid4)
    website = fields.ForeignKeyField(
        "engine_crawler.CrawlWebsiteModel",
        related_name="runs",
        on_delete=fields.CASCADE,
    )
    website_id: UUID
    requested_url = fields.CharField(max_length=2048)
    status = fields.CharField(max_length=24)
    config = fields.JSONField()
    progress = fields.JSONField(default=dict)
    error_code = fields.CharField(max_length=64, null=True)
    error_message = fields.TextField(null=True)
    started_at = fields.DatetimeField(null=True)
    finished_at = fields.DatetimeField(null=True)
    created_at = fields.DatetimeField(auto_now_add=True)

    pages: fields.ReverseRelation["CrawlPageModel"]
    findings: fields.ReverseRelation["CrawlFindingModel"]
    events: fields.ReverseRelation["CrawlEventModel"]

    class Meta(Model.Meta):
        table = "crawl_runs"


class CrawlPageModel(Model):
    id = fields.UUIDField(primary_key=True, default=uuid4)
    run = fields.ForeignKeyField(
        "engine_crawler.CrawlRunModel", related_name="pages", on_delete=fields.CASCADE
    )
    run_id: UUID
    url = fields.CharField(max_length=4096)
    normalized_url = fields.CharField(max_length=4096)
    depth = fields.IntField()
    status = fields.CharField(max_length=24)
    skip_reason = fields.CharField(max_length=64, null=True)
    discovery_source = fields.CharField(max_length=32)
    http_status = fields.IntField(null=True)
    content_type = fields.CharField(max_length=255, null=True)
    content_length = fields.IntField(null=True)
    final_url = fields.CharField(max_length=4096, null=True)
    title = fields.TextField(null=True)
    canonical_url = fields.CharField(max_length=4096, null=True)
    content_hash = fields.CharField(max_length=64, null=True)
    fetched_at = fields.DatetimeField(null=True)
    error_code = fields.CharField(max_length=64, null=True)
    error_message = fields.TextField(null=True)
    created_at = fields.DatetimeField(auto_now_add=True)

    outgoing_links: fields.ReverseRelation["CrawlLinkModel"]
    incoming_links: fields.ReverseRelation["CrawlLinkModel"]
    raw_html: fields.ReverseRelation["CrawlRawHTMLModel"]
    findings: fields.ReverseRelation["CrawlFindingModel"]
    events: fields.ReverseRelation["CrawlEventModel"]

    class Meta(Model.Meta):
        table = "crawl_pages"
        unique_together = (("run", "normalized_url"),)


class CrawlLinkModel(Model):
    id = fields.UUIDField(primary_key=True, default=uuid4)
    source = fields.ForeignKeyField(
        "engine_crawler.CrawlPageModel",
        related_name="outgoing_links",
        on_delete=fields.CASCADE,
    )
    target = fields.ForeignKeyField(
        "engine_crawler.CrawlPageModel",
        related_name="incoming_links",
        on_delete=fields.CASCADE,
    )
    anchor_text = fields.TextField(null=True)
    rel = fields.CharField(max_length=255, null=True)
    is_internal = fields.BooleanField()
    created_at = fields.DatetimeField(auto_now_add=True)

    class Meta(Model.Meta):
        table = "crawl_links"
        unique_together = (("source", "target"),)


class CrawlRawHTMLModel(Model):
    id = fields.UUIDField(primary_key=True, default=uuid4)
    page = fields.OneToOneField(
        "engine_crawler.CrawlPageModel",
        related_name="raw_html",
        on_delete=fields.CASCADE,
    )
    html = fields.TextField()
    stored_at = fields.DatetimeField(auto_now_add=True)

    class Meta(Model.Meta):
        table = "crawl_raw_html"


class CrawlFindingModel(Model):
    id = fields.UUIDField(primary_key=True, default=uuid4)
    website = fields.ForeignKeyField(
        "engine_crawler.CrawlWebsiteModel",
        related_name="findings",
        on_delete=fields.CASCADE,
    )
    run = fields.ForeignKeyField(
        "engine_crawler.CrawlRunModel",
        related_name="findings",
        on_delete=fields.CASCADE,
    )
    page = fields.ForeignKeyField(
        "engine_crawler.CrawlPageModel",
        related_name="findings",
        null=True,
        on_delete=fields.CASCADE,
    )
    group = fields.CharField(max_length=32)
    key = fields.CharField(max_length=64)
    value = fields.TextField()
    extra = fields.JSONField(null=True)
    created_at = fields.DatetimeField(auto_now_add=True)

    class Meta(Model.Meta):
        table = "crawl_findings"
        indexes = (("run", "group", "key"),)


class CrawlEventModel(Model):
    id = fields.BigIntField(primary_key=True, generated=True)
    run = fields.ForeignKeyField(
        "engine_crawler.CrawlRunModel", related_name="events", on_delete=fields.CASCADE
    )
    page = fields.ForeignKeyField(
        "engine_crawler.CrawlPageModel",
        related_name="events",
        null=True,
        on_delete=fields.CASCADE,
    )
    step = fields.CharField(max_length=48)
    level = fields.CharField(max_length=16)
    message = fields.TextField()
    data = fields.JSONField(null=True)
    created_at = fields.DatetimeField(auto_now_add=True)

    class Meta(Model.Meta):
        table = "crawl_events"
