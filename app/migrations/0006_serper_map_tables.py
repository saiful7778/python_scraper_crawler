import functools
from json import dumps, loads
from uuid import uuid4

from tortoise import fields, migrations
from tortoise.fields.base import OnDelete
from tortoise.migrations import operations as ops


class Migration(migrations.Migration):
    dependencies = [("models", "0006_add_timestamps_to_hours")]

    initial = False

    operations = [
        ops.CreateModel(
            name="SerperMapJobModel",
            fields=[
                (
                    "id",
                    fields.UUIDField(
                        primary_key=True, default=uuid4, unique=True, db_index=True
                    ),
                ),
                ("name", fields.CharField(max_length=200)),
                ("status", fields.CharField(max_length=16, db_index=True)),
                ("error", fields.TextField(default="", unique=False)),
                (
                    "config",
                    fields.JSONField(
                        encoder=functools.partial(dumps, separators=(",", ":")),
                        decoder=loads,
                    ),
                ),
                ("config_key", fields.CharField(max_length=64, db_index=True)),
                ("key_count", fields.IntField(default=0)),
                ("total_requests", fields.IntField(default=0)),
                ("done_requests", fields.IntField(default=0)),
                ("failed_requests", fields.IntField(default=0)),
                ("places_found", fields.IntField(default=0)),
                ("places_saved", fields.IntField(default=0)),
                ("credits_spent", fields.IntField(default=0)),
                ("created_at", fields.DatetimeField(auto_now=False, auto_now_add=True)),
                ("updated_at", fields.DatetimeField(auto_now=True, auto_now_add=False)),
            ],
            options={"table": "serper_map_jobs", "app": "models", "pk_attr": "id"},
            bases=["Model"],
        ),
        ops.CreateModel(
            name="SerperMapRequestModel",
            fields=[
                (
                    "id",
                    fields.UUIDField(
                        primary_key=True, default=uuid4, unique=True, db_index=True
                    ),
                ),
                (
                    "job",
                    fields.ForeignKeyField(
                        "models.SerperMapJobModel",
                        source_field="job_id",
                        db_constraint=True,
                        to_field="id",
                        related_name="requests",
                        on_delete=OnDelete.CASCADE,
                    ),
                ),
                ("keyword", fields.CharField(max_length=500)),
                ("page", fields.IntField()),
                ("grid_x", fields.IntField(default=0)),
                ("grid_y", fields.IntField(default=0)),
                ("lat", fields.FloatField()),
                ("lng", fields.FloatField()),
                ("zoom", fields.IntField()),
                ("status", fields.CharField(max_length=16, db_index=True, default="pending")),
                ("attempts", fields.IntField(default=0)),
                ("key_hint", fields.IntField(null=True)),
                ("http_status", fields.IntField(null=True)),
                ("error", fields.TextField(default="", unique=False)),
                ("places", fields.IntField(default=0)),
                ("credits", fields.IntField(default=0)),
                ("created_at", fields.DatetimeField(auto_now=False, auto_now_add=True)),
                ("updated_at", fields.DatetimeField(auto_now=True, auto_now_add=False)),
            ],
            options={
                "table": "serper_map_requests",
                "app": "models",
                "unique_together": (("job_id", "keyword", "page", "grid_x", "grid_y"),),
                "pk_attr": "id",
            },
            bases=["Model"],
        ),
    ]
