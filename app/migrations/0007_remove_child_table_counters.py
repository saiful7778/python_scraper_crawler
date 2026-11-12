from tortoise import migrations
from tortoise.migrations import operations as ops


class Migration(migrations.Migration):
    dependencies = [('models', '0006_remove_google_map_profile_user_reviews_extended')]

    initial = False

    operations = [
        ops.RemoveField(model_name='GoogleMapProfileHourModel', name='counter'),
        ops.RemoveField(model_name='GoogleMapProfileReviewModel', name='counter'),
        ops.RemoveField(model_name='GoogleMapProfileImageModel', name='counter'),
        ops.RemoveField(model_name='LeadEmailModel', name='counter'),
        ops.RemoveField(model_name='LeadCategoryModel', name='counter'),
        ops.RemoveField(model_name='LeadSocialModel', name='counter'),
    ]
