from uuid import uuid4

from tortoise import fields
from tortoise.models import Model


class SerperMapJobStatus:
    """Lifecycle of one Serper maps job. A job that ran out of credits is resumable."""

    RUNNING = "running"
    EXHAUSTED = "exhausted"
    INTERRUPTED = "interrupted"
    FINISHED = "finished"
    FAILED = "failed"


class SerperMapRequestStatus:
    """One page request of a job."""

    PENDING = "pending"
    DONE = "done"
    FAILED = "failed"


class SerperMapJobModel(Model):
    """One scrape job: the config it ran with and the counters of its progress."""

    id = fields.UUIDField(primary_key=True, default=uuid4)
    name = fields.CharField(max_length=200)
    status = fields.CharField(max_length=16, db_index=True)
    error = fields.TextField(default="")
    config = fields.JSONField()
    #: Hash of the search config, so a rerun can find its own unfinished job.
    config_key = fields.CharField(max_length=64, db_index=True)
    key_count = fields.IntField(default=0)
    total_requests = fields.IntField(default=0)
    done_requests = fields.IntField(default=0)
    failed_requests = fields.IntField(default=0)
    places_found = fields.IntField(default=0)
    places_saved = fields.IntField(default=0)
    credits_spent = fields.IntField(default=0)
    created_at = fields.DatetimeField(auto_now_add=True)
    updated_at = fields.DatetimeField(auto_now=True)

    requests: fields.ReverseRelation["SerperMapRequestModel"]

    class Meta(Model.Meta):
        table = "serper_map_jobs"


class SerperMapRequestModel(Model):
    """One Serper /maps call: a keyword, a tile and a page."""

    id = fields.UUIDField(primary_key=True, default=uuid4)
    job = fields.ForeignKeyField(
        "models.SerperMapJobModel", related_name="requests", on_delete=fields.CASCADE
    )
    keyword = fields.CharField(max_length=500)
    page = fields.IntField()
    grid_x = fields.IntField(default=0)
    grid_y = fields.IntField(default=0)
    lat = fields.FloatField()
    lng = fields.FloatField()
    zoom = fields.IntField()
    status = fields.CharField(
        max_length=16, db_index=True, default=SerperMapRequestStatus.PENDING
    )
    attempts = fields.IntField(default=0)
    #: Index of the key that last answered this page, never the key itself.
    key_hint = fields.IntField(null=True)
    http_status = fields.IntField(null=True)
    error = fields.TextField(default="")
    places = fields.IntField(default=0)
    credits = fields.IntField(default=0)
    created_at = fields.DatetimeField(auto_now_add=True)
    updated_at = fields.DatetimeField(auto_now=True)

    class Meta(Model.Meta):
        table = "serper_map_requests"
        unique_together = (("job", "keyword", "page", "grid_x", "grid_y"),)
