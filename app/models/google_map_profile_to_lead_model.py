from tortoise import fields
from tortoise.models import Model


class GoogleMapProfileToLeadModel(Model):
    map_profile = fields.ForeignKeyField(
        "models.GoogleMapProfileModel",
        related_name="lead_link",
        on_delete=fields.CASCADE,
    )
    lead = fields.ForeignKeyField(
        "models.LeadModel", related_name="map_profile_link", on_delete=fields.CASCADE
    )

    class Meta(Model.Meta):
        table = "google_map_profile_to_lead"
        unique_together = (("map_profile_id", "lead_id"),)
        indexes = (("map_profile_id", "lead_id"),)
