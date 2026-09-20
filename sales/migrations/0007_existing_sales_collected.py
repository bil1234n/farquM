"""
Sales made before hand-overs were tracked are treated as fully collected.

Data only, in a migration of its own (see core/0003 for why that matters on
PostgreSQL).

WHY "COLLECTED" AND NOT "WAITING"
---------------------------------
Nothing recorded whether those goods left the yard, and for almost all of
them they did, long ago. Marking every old sale as waiting would bury the
stock keeper's queue under months of sales nobody is coming back for, and
the one real sale still waiting would be impossible to find among them. New
sales start as waiting; old ones are closed.

No Delivery rows are invented for them - there was no hand-over to record -
so the lines simply say everything was taken.
"""
from django.db import migrations
from django.db.models import F


def mark_collected(apps, schema_editor):
    TransactionItem = apps.get_model("sales", "TransactionItem")
    Transaction = apps.get_model("sales", "Transaction")
    TransactionItem.objects.update(quantity_delivered=F("quantity"))
    Transaction.objects.update(delivery_status="DELIVERED")


def unmark(apps, schema_editor):
    TransactionItem = apps.get_model("sales", "TransactionItem")
    Transaction = apps.get_model("sales", "Transaction")
    TransactionItem.objects.update(quantity_delivered=0)
    Transaction.objects.update(delivery_status="PENDING")


class Migration(migrations.Migration):

    dependencies = [
        ("sales", "0006_deliveries"),
    ]

    operations = [
        migrations.RunPython(mark_collected, unmark),
    ]
