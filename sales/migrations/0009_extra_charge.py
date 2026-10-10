import django.core.validators
from decimal import Decimal

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("sales", "0008_customer_photos"),
    ]

    operations = [
        migrations.AddField(
            model_name="transaction",
            name="extra_charge_amount",
            field=models.DecimalField(
                decimal_places=2,
                default=Decimal("0.00"),
                max_digits=14,
                validators=[django.core.validators.MinValueValidator(Decimal("0"))],
            ),
        ),
        migrations.AddField(
            model_name="transaction",
            name="extra_charge_label",
            field=models.CharField(
                blank=True,
                help_text="What the extra charge is for - transport, worker, loading...",
                max_length=120,
            ),
        ),
        migrations.AddField(
            model_name="transaction",
            name="extra_charge_on_debt",
            field=models.BooleanField(default=False),
        ),
    ]
