"""
Which branch a notification is about, so news from one yard is not read as
news about the other. Blank means the whole business. api/0003 files the
notifications already sent under the main branch.
"""

import core.branching
import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("api", "0001_initial"),
        ("core", "0010_option_branch_required"),
    ]

    operations = [
        migrations.AddField(
            model_name="notificationlog",
            name="branch",
            field=models.ForeignKey(
                blank=True,
                default=core.branching.current_branch_id,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="+",
                to="core.branch",
            ),
        ),
    ]
