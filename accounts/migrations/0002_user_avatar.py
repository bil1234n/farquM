"""
Profile photo for staff accounts.

Nullable and blank, so every existing user keeps working and simply falls
back to their initials until they upload something.

Where the file physically lands depends on config, not on this migration:
with Cloudinary credentials set it goes to Cloudinary, otherwise to local
MEDIA_ROOT. The column stores a path either way, which is why switching
backends later does not need a schema change - see the note on
User.avatar_url about rows that outlive their storage.
"""
import core.utils
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("accounts", "0001_initial"),
    ]

    operations = [
        migrations.AddField(
            model_name="user",
            name="avatar",
            field=models.ImageField(
                blank=True,
                help_text="Profile photo. Square images look best.",
                null=True,
                upload_to=core.utils.avatar_upload_path,
                validators=[core.utils.validate_avatar_file],
            ),
        ),
    ]
