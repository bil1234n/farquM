"""
The managed pick-lists: one table, partitioned by `group`.

The seed at the bottom is written with a data migration rather than a fixture
so a fresh database and an existing one end up the same, and so re-running it
is harmless: every row is matched on (group, label) before being created.
Entries an administrator has since removed stay removed - they are marked
inactive rather than deleted (see api/option_views.py), and this only creates
rows that are missing entirely.
"""
import django.db.models.deletion
import django.db.models.functions.text
from django.conf import settings
from django.db import migrations, models


def seed_options(apps, schema_editor):
    Option = apps.get_model("core", "Option")

    # Imported rather than copied: the catalogue in core/options.py is the
    # single list, and a migration holding a second copy is a second list that
    # goes stale the first time somebody edits one of them.
    from core.options import seed_pairs

    existing = {
        (row["group"], row["label"].lower())
        for row in Option.objects.values("group", "label")
    }

    Option.objects.bulk_create(
        [
            Option(
                group=group,
                label=label,
                sort_order=order,
                is_active=True,
                is_seeded=True,
            )
            for group, label, order in seed_pairs()
            if (group, label.lower()) not in existing
        ]
    )


def unseed_options(apps, schema_editor):
    """Remove only the rows this migration created. Typed-in entries stay."""
    Option = apps.get_model("core", "Option")
    Option.objects.filter(is_seeded=True).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0001_initial"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="Option",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True, db_index=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "group",
                    models.CharField(
                        db_index=True,
                        help_text="Which list this belongs to. See core/options.py.",
                        max_length=40,
                    ),
                ),
                ("label", models.CharField(max_length=120)),
                (
                    "sort_order",
                    models.PositiveSmallIntegerField(
                        default=100,
                        help_text="Lower sorts first. Ties fall back to the label.",
                    ),
                ),
                (
                    "is_active",
                    models.BooleanField(
                        db_index=True,
                        default=True,
                        help_text="Off hides it from new forms without touching "
                        "old records.",
                    ),
                ),
                (
                    "is_seeded",
                    models.BooleanField(
                        default=False,
                        help_text="Shipped with the system rather than typed in "
                        "by somebody.",
                    ),
                ),
                (
                    "use_count",
                    models.PositiveIntegerField(
                        default=0,
                        help_text="How often it has been picked. Drives "
                        "'most used first'.",
                    ),
                ),
                (
                    "created_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="options_added",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={"ordering": ["sort_order", "label"]},
        ),
        migrations.AddIndex(
            model_name="option",
            index=models.Index(
                fields=["group", "is_active", "sort_order"],
                name="option_group_active_idx",
            ),
        ),
        migrations.AddConstraint(
            model_name="option",
            constraint=models.UniqueConstraint(
                models.F("group"),
                django.db.models.functions.text.Lower("label"),
                name="option_label_unique_per_group",
            ),
        ),
        migrations.RunPython(seed_options, unseed_options),
    ]
