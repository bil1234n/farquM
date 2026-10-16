"""
Branches, part 1 of 3 for sales: an empty `branch` column on customers and sales.

Each branch keeps its own customers and its own sales. Lines, charges,
receipts and hand-overs reach their branch through the sale.

Filled in by sales/0014_into_main_branch and made required by sales/0015_branch_required. Three
migrations, not one, because PostgreSQL refuses a schema change in the same
transaction as rows written by Python (core.tests.MigrationHygieneTests).
"""
import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("sales", "0012_handover_breakage"),
        ("core", "0008_branches"),
    ]

    operations = [
        migrations.AddField(
            model_name="customer",
            name="branch",
            field=models.ForeignKey(
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="+",
                to="core.branch",
            ),
        ),
        migrations.AddField(
            model_name="transaction",
            name="branch",
            field=models.ForeignKey(
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="+",
                to="core.branch",
            ),
        ),
    ]
