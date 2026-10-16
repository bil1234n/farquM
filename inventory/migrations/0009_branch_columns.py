"""
Branches, part 1 of 3 for inventory: an empty `branch` column on categories, suppliers and products.

Each branch keeps its own catalogue: its own products and stock, and its own
category and supplier lists. See core/branching.py.

Filled in by inventory/0010_into_main_branch and made required by inventory/0011_branch_required. Three
migrations, not one, because PostgreSQL refuses a schema change in the same
transaction as rows written by Python (core.tests.MigrationHygieneTests).
"""
import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("inventory", "0008_restock_corrections"),
        ("core", "0008_branches"),
    ]

    operations = [
        migrations.AddField(
            model_name="category",
            name="branch",
            field=models.ForeignKey(
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="+",
                to="core.branch",
            ),
        ),
        migrations.AddField(
            model_name="supplier",
            name="branch",
            field=models.ForeignKey(
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="+",
                to="core.branch",
            ),
        ),
        migrations.AddField(
            model_name="product",
            name="branch",
            field=models.ForeignKey(
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="+",
                to="core.branch",
            ),
        ),
    ]
