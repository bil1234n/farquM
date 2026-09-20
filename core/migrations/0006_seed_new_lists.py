"""
Seed the lists added since core/0004 - note colours, expense categories,
employee payment types and jobs - and colour the note marks.

Data only. Re-runnable: it matches on (group, label) before creating, and
only sets a colour on an entry that has none, so a colour somebody has since
changed is left alone.
"""
from django.db import migrations


def seed(apps, schema_editor):
    Option = apps.get_model("core", "Option")

    # Imported rather than copied: core/options.py is the single catalogue.
    from core.options import seed_colors, seed_pairs

    by_key = {
        (row["group"], row["label"].lower()): row["id"]
        for row in Option.objects.values("id", "group", "label")
    }

    missing = []
    for group, code, label, order in seed_pairs():
        if (group, label.lower()) in by_key:
            continue
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
        by_key[(group, label.lower())] = None
    Option.objects.bulk_create(missing)

    for group, label, color in seed_colors():
        Option.objects.filter(
            group=group, label__iexact=label, color=""
        ).update(color=color)


def unseed(apps, schema_editor):
    """Remove the seeded rows of the new lists. Typed-in entries stay."""
    Option = apps.get_model("core", "Option")
    Option.objects.filter(
        group__in=["NOTE_TAG", "EXPENSE_CATEGORY", "EMPLOYEE_PAY_TYPE", "EMPLOYEE_JOB"],
        is_seeded=True,
    ).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0005_option_color"),
    ]

    operations = [
        migrations.RunPython(seed, unseed),
    ]
