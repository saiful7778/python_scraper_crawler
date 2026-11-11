from tortoise import migrations
from tortoise.migrations import operations as ops

TABLE = "google_maps_profiles"


def _backfill_star_counts() -> str:
    parts = []
    for star in range(1, 6):
        parts.append(
            f"UPDATE {TABLE} SET review_star_{star}_count = "
            f"(reviews_per_rating::jsonb ->> '{star}')::int "
            f"WHERE review_star_{star}_count IS NULL "
            f"AND reviews_per_rating ~ '^\\s*\\{{' "
            f"AND (reviews_per_rating::jsonb ->> '{star}') ~ '^[0-9]+$';"
        )
    return "\n".join(parts)


class Migration(migrations.Migration):
    dependencies = [('models', '0003_google_map_profile_json_fields')]

    initial = False

    operations = [
        ops.RunSQL(
            sql=_backfill_star_counts(),
            reverse_sql=f"ALTER TABLE {TABLE} ADD COLUMN IF NOT EXISTS reviews_per_rating TEXT NOT NULL DEFAULT ''",
        ),
        ops.RemoveField(model_name='GoogleMapProfileModel', name='reviews_per_rating'),
    ]
