import django.db.models.deletion
from django.db import migrations, models


def lines_from_single_charge(apps, schema_editor):
    """
    Sales saved with the one-extra-charge columns get a matching line, so
    every sale reads the same way from here on.
    """
    Transaction = apps.get_model("sales", "Transaction")
    ExtraCharge = apps.get_model("sales", "ExtraCharge")

    rows = []
    for txn in Transaction.objects.filter(extra_charge_amount__gt=0).only(
        "id", "extra_charge_amount", "extra_charge_label", "extra_charge_on_debt"
    ):
        rows.append(
            ExtraCharge(
                transaction_id=txn.id,
                label=(txn.extra_charge_label or "Extra charge")[:120],
                amount=txn.extra_charge_amount,
                on_debt=txn.extra_charge_on_debt,
            )
        )
    ExtraCharge.objects.bulk_create(rows)


class Migration(migrations.Migration):

    dependencies = [
        ("sales", "0009_extra_charge"),
    ]

    operations = [
        migrations.CreateModel(
            name="ExtraCharge",
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
                ("label", models.CharField(max_length=120)),
                ("amount", models.DecimalField(decimal_places=2, max_digits=14)),
                ("on_debt", models.BooleanField(default=False)),
                (
                    "transaction",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="extra_charges",
                        to="sales.transaction",
                    ),
                ),
            ],
            options={
                "ordering": ["id"],
            },
        ),
        migrations.AddConstraint(
            model_name="extracharge",
            constraint=models.CheckConstraint(
                condition=models.Q(("amount__gt", 0)),
                name="extra_charge_amount_positive",
            ),
        ),
        migrations.RunPython(lines_from_single_charge, migrations.RunPython.noop),
    ]
