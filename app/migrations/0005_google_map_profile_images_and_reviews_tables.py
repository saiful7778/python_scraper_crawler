import hashlib
import json
import uuid

from tortoise import connections, migrations
from tortoise.migrations import operations as ops


def _as_list(value):
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return []
    return value if isinstance(value, list) else []


def _review_hash(review: dict) -> str:
    return hashlib.sha256(
        json.dumps(review, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()


async def _move_to_child_tables(apps, schema_editor):
    conn = connections.get("default")
    profiles = await conn.execute_query_dict(
        "SELECT id, images, user_reviews FROM google_maps_profiles"
    )
    for profile in profiles:
        google_map_id = profile["id"]
        images = [
            image
            for image in _as_list(profile["images"])
            if isinstance(image, dict) and image.get("image")
        ]
        for counter, image in enumerate(images):
            await conn.execute_query(
                "INSERT INTO google_map_profile_images "
                '(id, counter, "isThumbnail", url, title, created_at, updated_at, google_map_id) '
                "VALUES ($1, $2, false, $3, $4, now(), now(), $5) ON CONFLICT DO NOTHING",
                [uuid.uuid4(), counter, image["image"], image.get("title") or None, google_map_id],
            )
        reviews = [r for r in _as_list(profile["user_reviews"]) if isinstance(r, dict)]
        for counter, review in enumerate(reviews):
            await conn.execute_query(
                "INSERT INTO google_map_profile_reviews "
                "(id, counter, rating, when_text, description, content_hash, created_at, updated_at, google_map_id) "
                "VALUES ($1, $2, $3, $4, $5, $6, now(), now(), $7) ON CONFLICT DO NOTHING",
                [
                    uuid.uuid4(),
                    counter,
                    review.get("Rating"),
                    review.get("When") or None,
                    review.get("Description") or None,
                    _review_hash(review),
                    google_map_id,
                ],
            )


class Migration(migrations.Migration):
    dependencies = [('models', '0004_remove_google_map_profile_reviews_per_rating')]

    initial = False

    operations = [
        ops.RunPython(_move_to_child_tables),
        ops.RemoveField(model_name='GoogleMapProfileModel', name='images'),
        ops.RemoveField(model_name='GoogleMapProfileModel', name='user_reviews'),
    ]
