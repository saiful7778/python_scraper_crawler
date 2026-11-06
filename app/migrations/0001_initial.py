from tortoise import migrations
from tortoise.migrations import operations as ops
from tortoise.fields.base import OnDelete
from uuid import uuid4
from tortoise import fields

class Migration(migrations.Migration):
    initial = True

    operations = [
        ops.CreateModel(
            name='LeadModel',
            fields=[
                ('id', fields.UUIDField(primary_key=True, default=uuid4, unique=True, db_index=True)),
                ('counter', fields.IntField(null=True, db_index=True)),
                ('title', fields.CharField(null=True, db_index=True, max_length=500)),
                ('category', fields.TextField(null=True, unique=False)),
                ('address', fields.TextField(null=True, unique=False)),
                ('phone', fields.TextField(null=True, unique=False)),
                ('website', fields.TextField(null=True, unique=False)),
                ('status', fields.TextField(null=True, unique=False)),
                ('descriptions', fields.TextField(null=True, unique=False)),
                ('owner', fields.TextField(null=True, unique=False)),
                ('timezone', fields.TextField(null=True, unique=False)),
                ('created_at', fields.DatetimeField(auto_now=False, auto_now_add=True)),
                ('updated_at', fields.DatetimeField(auto_now=True, auto_now_add=False)),
            ],
            options={'table': 'leads', 'app': 'models', 'pk_attr': 'id', 'table_description': 'Lead Model'},
            bases=['Model'],
        ),
        ops.CreateModel(
            name='LeadAddressModel',
            fields=[
                ('id', fields.UUIDField(primary_key=True, default=uuid4, unique=True, db_index=True)),
                ('borough', fields.TextField(null=True, unique=False)),
                ('street', fields.TextField(null=True, unique=False)),
                ('city', fields.TextField(null=True, unique=False)),
                ('state', fields.TextField(null=True, unique=False)),
                ('postal_code', fields.TextField(null=True, unique=False)),
                ('country', fields.TextField(null=True, unique=False)),
                ('latitude', fields.FloatField(null=True)),
                ('longitude', fields.FloatField(null=True)),
                ('plus_code', fields.TextField(null=True, unique=False)),
                ('created_at', fields.DatetimeField(auto_now=False, auto_now_add=True)),
                ('updated_at', fields.DatetimeField(auto_now=True, auto_now_add=False)),
                ('lead', fields.OneToOneField('models.LeadModel', source_field='lead_id', db_constraint=True, to_field='id', related_name='detailed_address', on_delete=OnDelete.CASCADE)),
            ],
            options={'table': 'lead_addresses', 'app': 'models', 'pk_attr': 'id', 'table_description': 'Lead address model'},
            bases=['Model'],
        ),
        ops.CreateModel(
            name='LeadCategoryModel',
            fields=[
                ('id', fields.UUIDField(primary_key=True, default=uuid4, unique=True, db_index=True)),
                ('counter', fields.IntField(default=0)),
                ('name', fields.CharField(max_length=255)),
                ('created_at', fields.DatetimeField(auto_now=False, auto_now_add=True)),
                ('updated_at', fields.DatetimeField(auto_now=True, auto_now_add=False)),
                ('lead', fields.ForeignKeyField('models.LeadModel', source_field='lead_id', db_constraint=True, to_field='id', related_name='categories', on_delete=OnDelete.CASCADE)),
            ],
            options={'table': 'lead_categories', 'app': 'models', 'unique_together': (('lead', 'name'),), 'pk_attr': 'id', 'table_description': 'Lead category model'},
            bases=['Model'],
        ),
        ops.CreateModel(
            name='LeadEmailModel',
            fields=[
                ('id', fields.UUIDField(primary_key=True, default=uuid4, unique=True, db_index=True)),
                ('counter', fields.IntField(default=0)),
                ('email', fields.CharField(max_length=320)),
                ('created_at', fields.DatetimeField(auto_now=False, auto_now_add=True)),
                ('updated_at', fields.DatetimeField(auto_now=True, auto_now_add=False)),
                ('lead', fields.ForeignKeyField('models.LeadModel', source_field='lead_id', db_constraint=True, to_field='id', related_name='emails', on_delete=OnDelete.CASCADE)),
            ],
            options={'table': 'lead_emails', 'app': 'models', 'unique_together': (('lead', 'email'),), 'pk_attr': 'id', 'table_description': 'Lead email model'},
            bases=['Model'],
        ),
        ops.CreateModel(
            name='LeadSocialModel',
            fields=[
                ('id', fields.UUIDField(primary_key=True, default=uuid4, unique=True, db_index=True)),
                ('counter', fields.IntField(default=0)),
                ('platform', fields.CharField(max_length=16)),
                ('url', fields.TextField(unique=False)),
                ('created_at', fields.DatetimeField(auto_now=False, auto_now_add=True)),
                ('updated_at', fields.DatetimeField(auto_now=True, auto_now_add=False)),
                ('lead', fields.ForeignKeyField('models.LeadModel', source_field='lead_id', db_constraint=True, to_field='id', related_name='socials', on_delete=OnDelete.CASCADE)),
            ],
            options={'table': 'lead_socials', 'app': 'models', 'unique_together': (('lead', 'platform'),), 'pk_attr': 'id', 'table_description': 'Lead social model'},
            bases=['Model'],
        ),
    ]
