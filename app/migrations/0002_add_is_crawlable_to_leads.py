from tortoise import migrations
from tortoise.migrations import operations as ops
from tortoise import fields

class Migration(migrations.Migration):
    dependencies = [('models', '0001_initial')]

    initial = False

    operations = [
        ops.AddField(
            model_name='LeadModel',
            name='is_crawlable',
            field=fields.BooleanField(default=False, db_default=False),
        ),
    ]
