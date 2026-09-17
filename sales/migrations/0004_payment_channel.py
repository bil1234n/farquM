"""
Which bank, which wallet, which slip number.

'BANK' on its own cannot be reconciled against anything - somebody still has
to open the slip to find out whether it was CBE or Dashen, and by then the
slip is in a drawer. The link is for grouping; the name beside it is the
record, and survives the option being removed from the list later.
"""
import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("sales", "0003_alter_customer_owner_alter_transaction_owner"),
        ("core", "0002_option"),
    ]

    operations = [
        migrations.AddField(
            model_name="transaction",
            name="payment_channel",
            field=models.ForeignKey(
                blank=True,
                help_text="The bank or wallet the money came through.",
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="transactions",
                to="core.option",
            ),
        ),
        migrations.AddField(
            model_name="transaction",
            name="payment_channel_name",
            field=models.CharField(
                blank=True,
                db_index=True,
                help_text="What it was called at the time. Survives the option "
                "going.",
                max_length=120,
            ),
        ),
        migrations.AddField(
            model_name="transaction",
            name="payment_reference",
            field=models.CharField(
                blank=True,
                help_text="Transfer number, cheque number or wallet confirmation "
                "code.",
                max_length=80,
            ),
        ),
    ]
