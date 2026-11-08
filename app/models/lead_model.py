from uuid import uuid4

from tortoise import fields
from tortoise.models import Model


class LeadModel(Model):
    """Lead Model"""

    id = fields.UUIDField(primary_key=True, default=uuid4)
    counter = fields.IntField(null=True, db_index=True)

    title = fields.CharField(max_length=500, null=True, db_index=True)
    category = fields.TextField(null=True)
    address = fields.TextField(null=True)
    phone = fields.TextField(null=True)
    website = fields.TextField(null=True)
    website_domain = fields.CharField(max_length=255, null=True, unique=True)
    status = fields.TextField(null=True)
    descriptions = fields.TextField(null=True)
    owner = fields.TextField(null=True)
    timezone = fields.TextField(null=True)
    created_at = fields.DatetimeField(auto_now_add=True)
    updated_at = fields.DatetimeField(auto_now=True)

    class Meta(Model.Meta):
        table = "leads"


class LeadAddressModel(Model):
    """Lead address model"""

    id = fields.UUIDField(primary_key=True, default=uuid4)
    complete_address = fields.TextField(default="")
    plus_code = fields.TextField(default="")
    borough = fields.TextField(null=True)
    street = fields.TextField(null=True)
    city = fields.TextField(null=True)
    state = fields.TextField(null=True)
    postal_code = fields.TextField(null=True)
    country = fields.TextField(null=True)
    latitude = fields.FloatField(null=True)
    longitude = fields.FloatField(null=True)
    plus_code = fields.TextField(null=True)
    created_at = fields.DatetimeField(auto_now_add=True)
    updated_at = fields.DatetimeField(auto_now=True)
    lead = fields.OneToOneField(
        "models.LeadModel", related_name="detailed_address", on_delete=fields.CASCADE
    )

    class Meta(Model.Meta):
        table = "lead_addresses"


class LeadEmailModel(Model):
    """Lead email model"""

    id = fields.UUIDField(primary_key=True, default=uuid4)
    counter = fields.IntField(default=0)
    email = fields.CharField(max_length=320)
    created_at = fields.DatetimeField(auto_now_add=True)
    updated_at = fields.DatetimeField(auto_now=True)
    lead = fields.ForeignKeyField(
        "models.LeadModel", related_name="emails", on_delete=fields.CASCADE
    )

    class Meta(Model.Meta):
        table = "lead_emails"
        unique_together = (("lead", "email"),)


class LeadCategoryModel(Model):
    """Lead category model"""

    id = fields.UUIDField(primary_key=True, default=uuid4)
    counter = fields.IntField(default=0)
    name = fields.CharField(max_length=255)
    created_at = fields.DatetimeField(auto_now_add=True)
    updated_at = fields.DatetimeField(auto_now=True)
    lead = fields.ForeignKeyField(
        "models.LeadModel", related_name="categories", on_delete=fields.CASCADE
    )

    class Meta(Model.Meta):
        table = "lead_categories"
        unique_together = (("lead", "name"),)


class LeadSocialModel(Model):
    """Lead social model"""

    id = fields.UUIDField(primary_key=True, default=uuid4)
    counter = fields.IntField(default=0)
    platform = fields.CharField(max_length=16)
    url = fields.TextField()
    created_at = fields.DatetimeField(auto_now_add=True)
    updated_at = fields.DatetimeField(auto_now=True)
    lead = fields.ForeignKeyField(
        "models.LeadModel", related_name="socials", on_delete=fields.CASCADE
    )

    class Meta(Model.Meta):
        table = "lead_socials"
        unique_together = (("lead", "platform"),)
