"""
A colour on pick-list entries, for the note colours.

Schema only - the colours themselves are set by core/0006, in a migration of
its own. See core/0003 for why the two must not share a transaction on
PostgreSQL.
"""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0004_seed_option_codes"),
    ]

    operations = [
        migrations.AddField(
            model_name="option",
            name="color",
            field=models.CharField(
                blank=True,
                help_text="#RRGGBB for lists shown as a coloured mark.",
                max_length=7,
            ),
        ),
    ]
