"""
Branches, part 3 of 3 for production: every row now has a branch, so the column is
required, and what had to be unique is now unique within a branch.
"""
import django.db.models.deletion
from django.db import migrations, models

import core.branching


class Migration(migrations.Migration):

    dependencies = [
        ("production", "0008_into_main_branch"),
    ]

    operations = [
        migrations.AlterField(
            model_name="rawmaterial",
            name="branch",
            field=models.ForeignKey(
                default=core.branching.branch_for_new_row,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="+",
                to="core.branch",
            ),
        ),
        migrations.RemoveConstraint(
            model_name="rawmaterial",
            name="material_code_unique_per_owner",
        ),
        migrations.AddConstraint(
            model_name="rawmaterial",
            constraint=models.UniqueConstraint(
                fields=("branch", "owner", "code"),
                name="material_code_unique_per_branch_owner",
            ),
        ),
    ]
