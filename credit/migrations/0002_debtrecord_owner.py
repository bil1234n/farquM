"""
Per-manager ownership for debts.

A debt belongs to the manager who extended the credit - they are the person
who has to go and collect it. So the owner is copied from the originating
sale first, and only falls back to the customer's owner when the sale is
missing (hand-entered opening balances, for instance).
"""
from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


def backfill_owner(apps, schema_editor):
    DebtRecord = apps.get_model("credit", "DebtRecord")
    Transaction = apps.get_model("sales", "Transaction")
    Customer = apps.get_model("sales", "Customer")
    User = apps.get_model("accounts", "User")

    fallback = (
        User.objects.filter(role="ADMIN", is_active=True).order_by("pk").first()
        or User.objects.filter(is_superuser=True).order_by("pk").first()
        or User.objects.order_by("pk").first()
    )
    fallback_id = fallback.pk if fallback else None

    for debt in DebtRecord.objects.filter(owner__isnull=True).iterator():
        owner_id = None
        if debt.transaction_id:
            owner_id = (
                Transaction.objects.filter(pk=debt.transaction_id)
                .values_list("owner_id", flat=True)
                .first()
            )
        if owner_id is None and debt.customer_id:
            owner_id = (
                Customer.objects.filter(pk=debt.customer_id)
                .values_list("owner_id", flat=True)
                .first()
            )
        owner_id = owner_id or debt.created_by_id or fallback_id
        if owner_id is not None:
            DebtRecord.objects.filter(pk=debt.pk).update(owner_id=owner_id)


def unbackfill(apps, schema_editor):
    apps.get_model("credit", "DebtRecord").objects.update(owner=None)


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("credit", "0001_initial"),
        # The backfill reads sales.Transaction.owner, so the sales migration
        # that creates that column has to have run first.
        ("sales", "0002_owner_scoping"),
    ]

    operations = [
        migrations.AddField(
            model_name="debtrecord",
            name="owner",
            field=models.ForeignKey(
                blank=True,
                help_text=(
                    "The staff member this record belongs to. Managers only ever "
                    "see their own records; administrators see everyone's."
                ),
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="credit_debtrecord_owned",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.RunPython(backfill_owner, unbackfill),
        migrations.AddIndex(
            model_name="debtrecord",
            index=models.Index(
                fields=["owner", "status", "due_date"], name="debt_owner_status_idx"
            ),
        ),
    ]
