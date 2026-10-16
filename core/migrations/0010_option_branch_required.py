"""
Branches, part 3 of 3: every pick-list entry now has a branch, so the column
is required, and "unique label in a list" becomes "unique within a branch" -
the same bank may well be on both branches' lists.
"""
import django.db.models.deletion
import django.db.models.functions.text
from django.db import migrations, models

import core.branching


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0009_main_branch"),
    ]

    operations = [
        migrations.AlterField(
            model_name="option",
            name="branch",
            field=models.ForeignKey(
                default=core.branching.branch_for_new_row,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="+",
                to="core.branch",
            ),
        ),
        migrations.RemoveConstraint(
            model_name="option",
            name="option_label_unique_per_group",
        ),
        migrations.AddConstraint(
            model_name="option",
            constraint=models.UniqueConstraint(
                models.F("branch"),
                models.F("group"),
                django.db.models.functions.text.Lower("label"),
                name="option_label_unique_per_branch_group",
                violation_error_message="That entry is already in this list.",
            ),
        ),
    ]
