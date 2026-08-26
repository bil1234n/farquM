"""
Per-manager ownership for products.

Three things happen here, in this order:

  1. add Product.owner
  2. backfill it from created_by, so nobody loses sight of their own stock
  3. move SKU / barcode uniqueness from global to per-owner

Step 3 must come after step 2 only for clarity - the old data was globally
unique, so it cannot violate a weaker per-owner constraint either way.
"""
from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


def backfill_owner(apps, schema_editor):
    """
    Give every existing product an owner.

    Preference order:
      1. whoever created it (created_by)
      2. whoever last touched it (updated_by)
      3. the first administrator, so nothing is orphaned

    Leaving rows NULL would hide them from the manager who has been selling
    them all along, because core.scoping treats a NULL owner as admin-only.
    """
    Product = apps.get_model("inventory", "Product")
    User = apps.get_model("accounts", "User")

    fallback = (
        User.objects.filter(role="ADMIN", is_active=True).order_by("pk").first()
        or User.objects.filter(is_superuser=True).order_by("pk").first()
        or User.objects.order_by("pk").first()
    )
    fallback_id = fallback.pk if fallback else None

    for product in Product.objects.filter(owner__isnull=True).iterator():
        owner_id = product.created_by_id or product.updated_by_id or fallback_id
        if owner_id is not None:
            Product.objects.filter(pk=product.pk).update(owner_id=owner_id)


def unbackfill(apps, schema_editor):
    """Reverse migration: ownership simply stops mattering, so clear it."""
    Product = apps.get_model("inventory", "Product")
    Product.objects.update(owner=None)


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("inventory", "0001_initial"),
    ]

    operations = [
        migrations.AddField(
            model_name="product",
            name="owner",
            field=models.ForeignKey(
                blank=True,
                help_text=(
                    "The staff member this record belongs to. Managers only ever "
                    "see their own records; administrators see everyone's."
                ),
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="inventory_product_owned",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.RunPython(backfill_owner, unbackfill),
        migrations.AlterField(
            model_name="product",
            name="sku",
            field=models.CharField(
                db_index=True,
                help_text="Internal stock keeping unit. Auto-generated if left blank.",
                max_length=60,
            ),
        ),
        migrations.AlterField(
            model_name="product",
            name="barcode",
            field=models.CharField(
                blank=True,
                db_index=True,
                help_text="EAN/UPC scanned at the counter. Leave blank if none.",
                max_length=60,
                null=True,
            ),
        ),
        migrations.AddIndex(
            model_name="product",
            index=models.Index(
                fields=["owner", "is_active", "is_deleted"],
                name="product_owner_active_idx",
            ),
        ),
        migrations.AddConstraint(
            model_name="product",
            constraint=models.UniqueConstraint(
                fields=("owner", "sku"), name="product_sku_unique_per_owner"
            ),
        ),
        migrations.AddConstraint(
            model_name="product",
            constraint=models.UniqueConstraint(
                condition=models.Q(("barcode__isnull", False)),
                fields=("owner", "barcode"),
                name="product_barcode_unique_per_owner",
            ),
        ),
    ]
