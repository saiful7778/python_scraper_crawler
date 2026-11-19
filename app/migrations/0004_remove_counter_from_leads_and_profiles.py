from tortoise import migrations
from tortoise.migrations import operations as ops


class Migration(migrations.Migration):
    dependencies = [("models", "0003_remove_status_from_leads")]

    initial = False

    operations = [
        ops.RemoveField(model_name="LeadModel", name="counter"),
        ops.RemoveField(model_name="GoogleMapProfileModel", name="counter"),
    ]
