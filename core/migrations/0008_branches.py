"""
Branches, part 1 of 3: the branch table, and an empty `branch` column on the
pick-lists.

Each branch keeps its own lists from here on (core.branching). The column
arrives empty and nullable; core/0009 creates the MAIN branch - the one the
business has been running all along - and files every existing entry under
it; core/0010 makes the column required. Three migrations, not one, because
PostgreSQL refuses a schema change in the same transaction as rows written
by Python (see core.tests.MigrationHygieneTests).
"""
import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0007_seed_extra_charge_options"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="Branch",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True, db_index=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("name", models.CharField(max_length=120, unique=True)),
                ("address", models.CharField(blank=True, max_length=255)),
                ("phone", models.CharField(blank=True, max_length=40)),
                (
                    "is_active",
                    models.BooleanField(
                        db_index=True,
                        default=True,
                        help_text="Off for a branch that has closed. Its records stay.",
                    ),
                ),
                ("is_main", models.BooleanField(default=False, editable=False)),
                ("sort_order", models.PositiveIntegerField(default=0)),
            ],
            options={
                "verbose_name_plural": "Branches",
                "ordering": ["-is_main", "sort_order", "name"],
            },
        ),
        migrations.AddConstraint(
            model_name="branch",
            constraint=models.UniqueConstraint(
                condition=models.Q(("is_main", True)),
                fields=("is_main",),
                name="branch_one_main",
            ),
        ),
        # -- Pick-lists: one copy per branch ---------------------------------
        migrations.AddField(
            model_name="option",
            name="branch",
            field=models.ForeignKey(
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="+",
                to="core.branch",
            ),
        ),
    ]
