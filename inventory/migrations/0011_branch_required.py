"""
Branches, part 3 of 3 for inventory: every row now has a branch, so the column is
required, and what had to be unique is now unique within a branch.
"""
import django.db.models.deletion
from django.db import migrations, models

import core.branching


class Migration(migrations.Migration):

    dependencies = [
        ("inventory", "0010_into_main_branch"),
    ]

    operations = [
        migrations.AlterField(
            model_name="category",
            name="branch",
            field=models.ForeignKey(
                default=core.branching.branch_for_new_row,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="+",
                to="core.branch",
            ),
        ),
        migrations.AlterField(
            model_name="supplier",
            name="branch",
            field=models.ForeignKey(
                default=core.branching.branch_for_new_row,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="+",
                to="core.branch",
            ),
        ),
        migrations.AlterField(
            model_name="product",
            name="branch",
            field=models.ForeignKey(
                default=core.branching.branch_for_new_row,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="+",
                to="core.branch",
            ),
        ),
        # -- Unique within a branch ----------------------------------------
        migrations.RemoveConstraint(
            model_name="product",
            name="product_sku_unique_per_owner",
        ),
        migrations.RemoveConstraint(
            model_name="product",
            name="product_barcode_unique_per_owner",
        ),
        migrations.AlterField(
            model_name="category",
            name="name",
            field=models.CharField(max_length=120),
        ),
        migrations.AlterField(
            model_name="category",
            name="slug",
            field=models.SlugField(blank=True, max_length=140),
        ),
        migrations.AlterField(
            model_name="supplier",
            name="name",
            field=models.CharField(max_length=160),
        ),
        migrations.AddConstraint(
            model_name="category",
            constraint=models.UniqueConstraint(
                fields=("branch", "name"),
                name="category_name_unique_per_branch",
                violation_error_message="There is already a category with this name.",
            ),
        ),
        migrations.AddConstraint(
            model_name="category",
            constraint=models.UniqueConstraint(
                fields=("branch", "slug"),
                name="category_slug_unique_per_branch",
                violation_error_message="There is already a category with this name.",
            ),
        ),
        migrations.AddConstraint(
            model_name="supplier",
            constraint=models.UniqueConstraint(
                fields=("branch", "name"),
                name="supplier_name_unique_per_branch",
                violation_error_message="There is already a supplier with this name.",
            ),
        ),
        migrations.AddConstraint(
            model_name="product",
            constraint=models.UniqueConstraint(
                fields=("branch", "owner", "sku"),
                name="product_sku_unique_per_branch_owner",
            ),
        ),
        migrations.AddConstraint(
            model_name="product",
            constraint=models.UniqueConstraint(
                condition=models.Q(("barcode__isnull", False)),
                fields=("branch", "owner", "barcode"),
                name="product_barcode_unique_per_branch_owner",
            ),
        ),
    ]
