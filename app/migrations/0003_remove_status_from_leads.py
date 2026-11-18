from tortoise import migrations
from tortoise.migrations import operations as ops


class Migration(migrations.Migration):
    dependencies = [("models", "0002_add_is_crawlable_to_leads")]

    initial = False

    operations = [
        ops.RemoveField(model_name="LeadModel", name="status"),
    ]
