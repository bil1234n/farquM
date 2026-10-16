"""
Branches, part 3 of 3 for expenses: every row now has a branch, so the column is
required.
"""
import django.db.models.deletion
from django.db import migrations, models

import core.branching


class Migration(migrations.Migration):

    dependencies = [
        ("expenses", "0006_into_main_branch"),
    ]

    operations = [
        migrations.AlterField(
            model_name="employee",
            name="branch",
            field=models.ForeignKey(
                default=core.branching.branch_for_new_row,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="+",
                to="core.branch",
            ),
        ),
        migrations.AlterField(
            model_name="expense",
            name="branch",
            field=models.ForeignKey(
                default=core.branching.branch_for_new_row,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="+",
                to="core.branch",
            ),
        ),
    ]
