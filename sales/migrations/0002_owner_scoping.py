"""
Per-manager ownership for customers and sales.

Customer.phone stops being globally unique and becomes unique per owner.
That change is the whole point: two managers can legitimately serve the same
person, and a global constraint would tell the second one their customer's
phone number is "already taken" by a record they are not allowed to open.
"""
from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


def backfill_owner(apps, schema_editor):
    """
    Customers inherit their creator; sales inherit whoever rang them up.

    sold_by is the strongest possible signal for a transaction - it is the
    person who stood at the till - so it wins over created_by everywhere.
    """
    Customer = apps.get_model("sales", "Customer")
    Transaction = apps.get_model("sales", "Transaction")
    User = apps.get_model("accounts", "User")

    fallback = (
        User.objects.filter(role="ADMIN", is_active=True).order_by("pk").first()
        or User.objects.filter(is_superuser=True).order_by("pk").first()
        or User.objects.order_by("pk").first()
    )
    fallback_id = fallback.pk if fallback else None

    for customer in Customer.objects.filter(owner__isnull=True).iterator():
        owner_id = customer.created_by_id or customer.updated_by_id or fallback_id
        if owner_id is not None:
            Customer.objects.filter(pk=customer.pk).update(owner_id=owner_id)

    for txn in Transaction.objects.filter(owner__isnull=True).iterator():
        owner_id = txn.sold_by_id or fallback_id
        # A sale should sit with the same manager as its customer wherever
        # possible, otherwise the customer page and the sales list disagree.
        if owner_id is None and txn.customer_id:
            owner_id = (
                Customer.objects.filter(pk=txn.customer_id)
                .values_list("owner_id", flat=True)
                .first()
            )
        if owner_id is not None:
            Transaction.objects.filter(pk=txn.pk).update(owner_id=owner_id)


def unbackfill(apps, schema_editor):
    apps.get_model("sales", "Customer").objects.update(owner=None)
    apps.get_model("sales", "Transaction").objects.update(owner=None)


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("sales", "0001_initial"),
    ]

    operations = [
        migrations.AddField(
            model_name="customer",
            name="owner",
            field=models.ForeignKey(
                blank=True,
                help_text=(
                    "The staff member this record belongs to. Managers only ever "
                    "see their own records; administrators see everyone's."
                ),
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="sales_customer_owned",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AddField(
            model_name="transaction",
            name="owner",
            field=models.ForeignKey(
                blank=True,
                help_text=(
                    "The staff member this record belongs to. Managers only ever "
                    "see their own records; administrators see everyone's."
                ),
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="sales_transaction_owned",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.RunPython(backfill_owner, unbackfill),
        migrations.AlterField(
            model_name="customer",
            name="phone",
            field=models.CharField(
                db_index=True,
                help_text="Primary identifier for a customer within your own list.",
                max_length=30,
            ),
        ),
        migrations.AddIndex(
            model_name="customer",
            index=models.Index(
                fields=["owner", "is_active"], name="customer_owner_active_idx"
            ),
        ),
        migrations.AddIndex(
            model_name="transaction",
            index=models.Index(
                fields=["owner", "-created_at"], name="txn_owner_created_idx"
            ),
        ),
        migrations.AddConstraint(
            model_name="customer",
            constraint=models.UniqueConstraint(
                fields=("owner", "phone"), name="customer_phone_unique_per_owner"
            ),
        ),
    ]
