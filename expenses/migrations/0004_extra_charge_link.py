"""
The expense that records an extra charge's pay-out ("We paid out" on a sale)
points back at that charge, so the sale can show it and a profit can leave
it out of the running costs - it is already inside the cost of the sale.

Schema only: a nullable column, no rows written.
"""
import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("expenses", "0003_employee_photos"),
        ("sales", "0011_extra_charge_cost"),
    ]

    operations = [
        migrations.AddField(
            model_name="expense",
            name="extra_charge",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="expenses",
                to="sales.extracharge",
            ),
        ),
    ]
