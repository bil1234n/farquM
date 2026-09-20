"""
Seed the editable unit lists, and give existing rows the code they lack.

Data only - no schema changes - on purpose. The column this fills in is added
by core/0003 in a migration of its own; see that file for why the two cannot
share a transaction on PostgreSQL.

The seed is re-runnable: it matches on (group, label) before creating, and
only backfills a code onto rows that have none. A database that already ran
the old combined migration simply finds nothing left to do.
"""
from django.db import migrations


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
        ("core", "0003_option_code"),
    ]

    operations = [
        migrations.RunPython(seed_and_backfill, unseed),
    ]
