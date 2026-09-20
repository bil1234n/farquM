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

The seed below is re-runnable: it matches on (group, label) before creating, and
backfills the code onto rows a previous migration created without one.
"""
from django.db import migrations, models


def seed_and_backfill(apps, schema_editor):
    Option = apps.get_model("core", "Option")

    # Imported rather than copied: core/options.py is the single catalogue, and
    # a migration holding a second copy is a second list that goes stale.
    from core.options import seed_pairs

    by_key = {
        (row["group"], row["label"].lower()): row["id"]
        for row in Option.objects.values("id", "group", "label")
    }

    missing = []
    for group, code, label, order in seed_pairs():
        existing = by_key.get((group, label.lower()))
        if existing is None:
            missing.append(
                Option(
                    group=group,
                    code=code,
                    label=label,
                    sort_order=order,
                    is_active=True,
                    is_seeded=True,
                )
            )
        elif code:
            # A row core.0002 created before codes existed. Give it the one it
            # should have had, so products already pointing at PIECE resolve.
            Option.objects.filter(id=existing, code="").update(code=code)

    Option.objects.bulk_create(missing)


def unseed(apps, schema_editor):
    """Remove only the unit rows this migration added. Typed-in entries stay."""
    Option = apps.get_model("core", "Option")
    Option.objects.filter(
        group__in=["PRODUCT_UNIT", "MATERIAL_UNIT"], is_seeded=True
    ).delete()


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
        migrations.RunPython(seed_and_backfill, unseed),
    ]
