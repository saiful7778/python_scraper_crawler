from tortoise import fields, migrations
from tortoise.migrations import operations as ops


class Migration(migrations.Migration):
    """Give the hours table the timestamps every other profile table has.

    The columns are added nullable, filled from the day the row already had no
    timestamp for, and only then made NOT NULL, so the migration is safe on a
    table that already holds rows.
    """

    dependencies = [("models", "0005_remove_unique_idx")]

    initial = False

    operations = [
        ops.AddField(
            model_name="GoogleMapProfileHourModel",
            name="created_at",
            field=fields.DatetimeField(null=True),
        ),
        ops.AddField(
            model_name="GoogleMapProfileHourModel",
            name="updated_at",
            field=fields.DatetimeField(null=True),
        ),
        ops.RunSQL(
            "UPDATE google_map_profile_hours SET created_at = CURRENT_TIMESTAMP "
            "WHERE created_at IS NULL",
            reverse_sql=ops.RunSQL.noop,
        ),
        ops.RunSQL(
            "UPDATE google_map_profile_hours SET updated_at = created_at "
            "WHERE updated_at IS NULL",
            reverse_sql=ops.RunSQL.noop,
        ),
        ops.AlterField(
            model_name="GoogleMapProfileHourModel",
            name="created_at",
            field=fields.DatetimeField(auto_now_add=True),
        ),
        ops.AlterField(
            model_name="GoogleMapProfileHourModel",
            name="updated_at",
            field=fields.DatetimeField(auto_now=True),
        ),
    ]
