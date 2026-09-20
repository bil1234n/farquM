"""
Room for a unit somebody typed, and no list bolted to the column.

10 characters was enough for the shipped codes (PIECE, CARTON). It is not
enough for one a yard adds itself - WHEELBARROW is eleven - and a unit
truncated on save renders as nothing on every row using it.

`choices` comes OFF the field at the same time. Leaving it on would have
Model.full_clean() refuse every unit added since deploy: the product form
posting PALLET was told "Select a valid choice" even though PALLET was in the
dropdown it came from. The shipped list still exists as Product.Unit - it
seeds the editable list and supplies fallback wording - it simply no longer
decides what may be stored. See core.forms.unit_choices and
api.serializers._validate_unit for what does.
"""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("inventory", "0004_alter_stockmovement_movement_type"),
        ("core", "0003_option_code"),
    ]

    operations = [
        migrations.AlterField(
            model_name="product",
            name="unit",
            field=models.CharField(default="PIECE", max_length=32),
        ),
    ]
