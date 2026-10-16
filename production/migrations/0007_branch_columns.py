"""
Branches, part 1 of 3 for production: an empty `branch` column on raw materials.

Each branch keeps its own store of materials. Movements, recipes and batches
reach their branch through the material or product they belong to.

Filled in by production/0008_into_main_branch and made required by production/0009_branch_required. Three
migrations, not one, because PostgreSQL refuses a schema change in the same
transaction as rows written by Python (core.tests.MigrationHygieneTests).
"""
import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("production", "0006_delivery_corrections"),
        ("core", "0008_branches"),
    ]

    operations = [
        migrations.AddField(
            model_name="rawmaterial",
            name="branch",
            field=models.ForeignKey(
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="+",
                to="core.branch",
            ),
        ),
    ]
