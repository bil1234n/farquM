"""
Codes on the pick-lists, so a unit list can be edited.

WHY A CODE AND NOT JUST THE LABEL
---------------------------------
Most of these lists are referenced by id with a snapshot of the wording - a
bank, a damage type - and that is right for them: removing one must not rewrite
what an old sale said.

A unit is the opposite case. Every product row already holds "PIECE" in its own
column, and the whole point of making the list editable is that renaming "Piece"
to "Each" should re-word every product at once. That needs a stable identifier
underneath the wording, which is what `code` is.

WHY THE SEED IS NOT IN HERE
---------------------------
This migration only adds the column. Filling it in is core/0004, on its own.

They used to be one migration, and on PostgreSQL that cannot work: the seed
inserts rows into core_option, whose foreign key to the user table is checked
at COMMIT, so those checks are still queued when Django creates this column's
index at the end of the same migration - and PostgreSQL refuses:

    cannot CREATE INDEX "core_option" because it has pending trigger events

Each migration runs in its own transaction, so splitting them means the index
is built and committed before a single row is written. SQLite, which the test
suite used to run on, has no such rule, which is how it got through.
"""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0002_option"),
    ]

    operations = [
        migrations.AddField(
            model_name="option",
            name="code",
            field=models.CharField(
                blank=True,
                db_index=True,
                help_text="Stable identifier for lists other tables store by "
                "code.",
                max_length=32,
            ),
        ),
    ]
