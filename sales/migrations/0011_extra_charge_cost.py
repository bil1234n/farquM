"""
What the business paid out for an extra charge on a sale ("We paid out").

Schema only: two columns and a check, no rows written. Each pay-out is also
an ordinary expense, linked back by expenses/0004; the sale keeps the total
of its pay-outs so a profit is one subtraction, not a join. Existing extra
charges start at zero - nothing was recorded as paid out for them.
"""
from decimal import Decimal

import django.core.validators
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("sales", "0010_extracharge_lines"),
    ]

    operations = [
        migrations.AddField(
            model_name="transaction",
            name="extra_charge_cost",
            field=models.DecimalField(
                decimal_places=2,
                default=Decimal("0.00"),
                max_digits=14,
                validators=[django.core.validators.MinValueValidator(Decimal("0"))],
            ),
        ),
        migrations.AddField(
            model_name="extracharge",
            name="cost_amount",
            field=models.DecimalField(
                decimal_places=2,
                default=Decimal("0.00"),
                help_text="What the business paid out for it. Zero when nothing left.",
                max_digits=14,
                validators=[django.core.validators.MinValueValidator(Decimal("0"))],
            ),
        ),
        migrations.AddConstraint(
            model_name="extracharge",
            constraint=models.CheckConstraint(
                condition=models.Q(("cost_amount__gte", 0)),
                name="extra_charge_cost_not_negative",
            ),
        ),
    ]
