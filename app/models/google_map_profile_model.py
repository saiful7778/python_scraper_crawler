from uuid import uuid4

from tortoise import fields
from tortoise.models import Model


class GoogleMapProfileModel(Model):
    id = fields.UUIDField(primary_key=True, default=uuid4)
    counter = fields.IntField(default=0)
    data_id = fields.TextField(null=True)
    place_id = fields.TextField(null=True)
    cid = fields.TextField(null=True)
    link = fields.TextField(null=True)
    price_range = fields.TextField(null=True)
    credit_cards_accepted = fields.TextField(null=True)
    reviews_link = fields.TextField(null=True)
    street_view_url = fields.TextField(null=True)
    about = fields.JSONField(null=True)
    input_id = fields.TextField(default="")
    reviews_per_rating = fields.TextField(default="")
    open_hours = fields.TextField(default="")
    popular_times = fields.TextField(default="")
    thumbnail = fields.TextField(default="")
    reservations = fields.TextField(default="")
    order_online = fields.TextField(default="")
    menu = fields.TextField(default="")
    images = fields.TextField(default="")
    user_reviews = fields.TextField(default="")
    user_reviews_extended = fields.TextField(default="")

    review_count = fields.IntField(null=True)
    review_rating = fields.FloatField(null=True)
    review_star_1_count = fields.IntField(null=True)
    review_star_2_count = fields.IntField(null=True)
    review_star_3_count = fields.IntField(null=True)
    review_star_4_count = fields.IntField(null=True)
    review_star_5_count = fields.IntField(null=True)

    created_at = fields.DatetimeField(auto_now_add=True)
    updated_at = fields.DatetimeField(auto_now=True)

    class Meta(Model.Meta):
        table = "google_maps_profiles"


class GoogleMapProfileHourModel(Model):
    id = fields.UUIDField(primary_key=True, default=uuid4)
    counter = fields.IntField(default=0)
    day = fields.CharField(max_length=16)
    position = fields.IntField(default=0)
    span = fields.TextField(null=True)
    google_map = fields.ForeignKeyField(
        "models.GoogleMapProfileModel", related_name="hours", on_delete=fields.CASCADE
    )

    class Meta(Model.Meta):
        table = "google_map_profile_hours"
        unique_together = (("google_map", "day", "position"),)


class GoogleMapProfileReviewModel(Model):
    id = fields.UUIDField(primary_key=True, default=uuid4)
    counter = fields.IntField(default=0)
    rating = fields.IntField(null=True)
    when_text = fields.TextField(null=True)
    description = fields.TextField(null=True)
    content_hash = fields.CharField(max_length=64)
    created_at = fields.DatetimeField(auto_now_add=True)
    updated_at = fields.DatetimeField(auto_now=True)
    google_map = fields.ForeignKeyField(
        "models.GoogleMapProfileModel", related_name="reviews", on_delete=fields.CASCADE
    )

    class Meta(Model.Meta):
        table = "google_map_profile_reviews"
        unique_together = (("google_map", "content_hash"),)


class GoogleMapProfileImageModel(Model):
    id = fields.UUIDField(primary_key=True, default=uuid4)
    counter = fields.IntField(default=0)
    isThumbnail = fields.BooleanField(default=False)
    url = fields.TextField()
    title = fields.TextField(null=True)
    created_at = fields.DatetimeField(auto_now_add=True)
    updated_at = fields.DatetimeField(auto_now=True)
    google_map = fields.ForeignKeyField(
        "models.GoogleMapProfileModel", related_name="image_rows", on_delete=fields.CASCADE
    )

    class Meta(Model.Meta):
        table = "google_map_profile_images"
        unique_together = (("google_map", "url"),)


class KeywordModel(Model):
    id = fields.UUIDField(primary_key=True, default=uuid4)
    counter = fields.IntField(null=True, db_index=True)
    name = fields.CharField(max_length=500)
    name_key = fields.CharField(max_length=500, unique=True)
    created_at = fields.DatetimeField(auto_now_add=True)
    updated_at = fields.DatetimeField(auto_now=True)

    class Meta(Model.Meta):
        table = "keywords"


class GoogleMapKeyword(Model):
    id = fields.UUIDField(primary_key=True, default=uuid4)
    counter = fields.IntField(default=0)
    job_id = fields.CharField(max_length=64, default="", db_index=True)
    created_at = fields.DatetimeField(auto_now_add=True)
    updated_at = fields.DatetimeField(auto_now=True)
    google_map = fields.ForeignKeyField(
        "models.GoogleMapProfileModel",
        related_name="keyword_links",
        on_delete=fields.CASCADE,
    )
    keyword = fields.ForeignKeyField(
        "models.KeywordModel", related_name="google_map_links", on_delete=fields.CASCADE
    )

    class Meta(Model.Meta):
        table = "google_map_keywords"
        unique_together = (("google_map", "keyword"),)
