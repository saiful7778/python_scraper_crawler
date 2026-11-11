from uuid import uuid4

from tortoise import fields
from tortoise.models import Model


class ScrapeModel(Model):
    id = fields.UUIDField(primary_key=True, default=uuid4)
    name = fields.CharField(max_length=200)
    status = fields.CharField(max_length=16, db_index=True)
    error = fields.TextField(default="")
    created_order = fields.IntField(db_index=True)
    settings = fields.JSONField()
    keywords = fields.JSONField()
    progress = fields.JSONField(default=dict)
    resumes = fields.IntField(default=0)
    created_at = fields.DatetimeField(auto_now_add=True)
    updated_at = fields.DatetimeField(auto_now=True)

    class Meta(Model.Meta):
        table = "scrapes"
