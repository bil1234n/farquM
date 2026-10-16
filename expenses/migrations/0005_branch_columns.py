"""
Branches, part 1 of 3 for expenses: an empty `branch` column on employees and expenses.

Each branch keeps its own payroll and its own money out.

Filled in by expenses/0006_into_main_branch and made required by expenses/0007_branch_required. Three
migrations, not one, because PostgreSQL refuses a schema change in the same
transaction as rows written by Python (core.tests.MigrationHygieneTests).
"""
import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("expenses", "0004_extra_charge_link"),
        ("core", "0008_branches"),
    ]

    operations = [
        migrations.AddField(
            model_name="employee",
            name="branch",
            field=models.ForeignKey(
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="+",
                to="core.branch",
            ),
        ),
        migrations.AddField(
            model_name="expense",
            name="branch",
            field=models.ForeignKey(
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="+",
                to="core.branch",
            ),
        ),
    ]
