from tortoise import migrations
from tortoise.migrations import operations as ops
import functools
from json import dumps, loads
from uuid import uuid4
from tortoise import fields

class Migration(migrations.Migration):
    dependencies = [('models', '0001_initial')]

    initial = False

    operations = [
        ops.CreateModel(
            name='ScrapeModel',
            fields=[
                ('id', fields.UUIDField(primary_key=True, default=uuid4, unique=True, db_index=True)),
                ('name', fields.CharField(max_length=200)),
                ('status', fields.CharField(db_index=True, max_length=16)),
                ('error', fields.TextField(default='', unique=False)),
                ('created_order', fields.IntField(db_index=True)),
                ('settings', fields.JSONField(encoder=functools.partial(dumps, separators=(',', ':')), decoder=loads)),
                ('keywords', fields.JSONField(encoder=functools.partial(dumps, separators=(',', ':')), decoder=loads)),
                ('progress', fields.JSONField(default=dict, encoder=functools.partial(dumps, separators=(',', ':')), decoder=loads)),
                ('resumes', fields.IntField(default=0)),
                ('created_at', fields.DatetimeField(auto_now=False, auto_now_add=True)),
                ('updated_at', fields.DatetimeField(auto_now=True, auto_now_add=False)),
            ],
            options={'table': 'scrapes', 'app': 'models', 'pk_attr': 'id'},
            bases=['Model'],
        ),
        ops.DeleteModel(name='CrawlRawHTMLModel'),
    ]
