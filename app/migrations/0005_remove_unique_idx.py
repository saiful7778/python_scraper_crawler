from tortoise import migrations
from tortoise.migrations import operations as ops
from tortoise import fields

class Migration(migrations.Migration):
    dependencies = [('models', '0004_remove_counter_from_leads_and_profiles')]

    initial = False

    operations = [
        ops.AlterField(
            model_name='LeadModel',
            name='website_domain',
            field=fields.CharField(null=True, max_length=255),
        ),
    ]
