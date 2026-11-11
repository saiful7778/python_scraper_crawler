from tortoise import migrations
from tortoise.migrations import operations as ops
from tortoise import fields

class Migration(migrations.Migration):
    dependencies = [('models', '0002_scrapes_drop_raw_html')]

    initial = False

    operations = [
        ops.RemoveConstraint(
            model_name='CrawlWebsiteModel',
            name=None,
            fields=['scrape_id', 'lead_position'],
        ),
        ops.AlterField(
            model_name='CrawlWebsiteModel',
            name='origin',
            field=fields.CharField(unique=True, max_length=2048),
        ),
        ops.RemoveField(model_name='CrawlWebsiteModel', name='lead_position'),
        ops.RemoveField(model_name='CrawlWebsiteModel', name='scrape_id'),
    ]
