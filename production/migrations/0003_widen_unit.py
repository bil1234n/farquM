"""
Room for a unit somebody typed - the store's half of inventory/0005.

See that migration for why 10 characters stopped being enough and why the
shipped list no longer sits on the column, and RawMaterial.get_unit_display
for how the wording is resolved now.
"""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("production", "0002_damage_and_requests"),
        ("core", "0003_option_code"),
    ]

    operations = [
        migrations.AlterField(
            model_name="rawmaterial",
            name="unit",
            field=models.CharField(default="KG", max_length=32),
        ),
    ]
