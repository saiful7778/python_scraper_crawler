from tortoise import migrations
from tortoise.migrations import operations as ops
import functools
from json import dumps, loads
from tortoise import fields

COLUMNS = ("open_hours", "reservations", "order_online", "images")
TABLE = "google_maps_profiles"


def _to_json() -> str:
    parts = []
    for column in COLUMNS:
        parts.append(f"ALTER COLUMN {column} DROP DEFAULT")
        parts.append(
            f"ALTER COLUMN {column} TYPE JSONB USING NULLIF({column}, '')::jsonb"
        )
        parts.append(f"ALTER COLUMN {column} DROP NOT NULL")
    return f"ALTER TABLE {TABLE} " + ", ".join(parts)


def _to_text() -> str:
    parts = []
    for column in COLUMNS:
        parts.append(
            f"ALTER COLUMN {column} TYPE TEXT USING COALESCE({column}::text, '')"
        )
        parts.append(f"ALTER COLUMN {column} SET DEFAULT ''")
        parts.append(f"ALTER COLUMN {column} SET NOT NULL")
    return f"ALTER TABLE {TABLE} " + ", ".join(parts)


class Migration(migrations.Migration):
    dependencies = [('models', '0002_remove_lead_category')]

    initial = False

    operations = [
        ops.RunSQL(sql=_to_json(), reverse_sql=_to_text()),
        ops.AlterField(
            model_name='GoogleMapProfileModel',
            name='images',
            field=fields.JSONField(null=True, encoder=functools.partial(dumps, separators=(',', ':')), decoder=loads),
        ),
        ops.AlterField(
            model_name='GoogleMapProfileModel',
            name='open_hours',
            field=fields.JSONField(null=True, encoder=functools.partial(dumps, separators=(',', ':')), decoder=loads),
        ),
        ops.AlterField(
            model_name='GoogleMapProfileModel',
            name='order_online',
            field=fields.JSONField(null=True, encoder=functools.partial(dumps, separators=(',', ':')), decoder=loads),
        ),
        ops.AlterField(
            model_name='GoogleMapProfileModel',
            name='reservations',
            field=fields.JSONField(null=True, encoder=functools.partial(dumps, separators=(',', ':')), decoder=loads),
        ),
    ]
