from tortoise import migrations
from tortoise.migrations import operations as ops


class Migration(migrations.Migration):
    dependencies = [('models', '0005_google_map_profile_images_and_reviews_tables')]

    initial = False

    operations = [
        ops.RemoveField(model_name='GoogleMapProfileModel', name='user_reviews_extended'),
    ]
