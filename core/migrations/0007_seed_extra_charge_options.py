"""
Seed the EXTRA_CHARGE pick-list - what an extra charge on a sale was for
(transport, a loading worker...).

Data only. Re-runnable: it matches on (group, label) before creating, so an
entry somebody already added by hand is not duplicated.
"""
from django.db import migrations

GROUP = "EXTRA_CHARGE"


def seed(apps, schema_editor):
    Option = apps.get_model("core", "Option")

    # Imported rather than copied: core/options.py is the single catalogue.
    from core.options import seed_pairs

    existing = {
        label.lower()
        for label in Option.objects.filter(group=GROUP).values_list("label", flat=True)
    }

    Option.objects.bulk_create(
        [
            Option(
                group=group,
                code=code,
                label=label,
                sort_order=order,
                is_active=True,
                is_seeded=True,
            )
            for group, code, label, order in seed_pairs()
            if group == GROUP and label.lower() not in existing
        ]
    )


def unseed(apps, schema_editor):
    """Remove the seeded rows. Typed-in entries stay."""
    Option = apps.get_model("core", "Option")
    Option.objects.filter(group=GROUP, is_seeded=True).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0006_seed_new_lists"),
    ]

    operations = [
        migrations.RunPython(seed, unseed),
    ]
