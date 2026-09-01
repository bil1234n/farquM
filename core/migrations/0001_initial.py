"""Editable business settings - one row, created on first access."""
import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    initial = True

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="SystemSetting",
            fields=[
                (
                    "id",
                    models.PositiveSmallIntegerField(
                        default=1, primary_key=True, serialize=False
                    ),
                ),
                ("business_name", models.CharField(blank=True, max_length=120)),
                ("business_phone", models.CharField(blank=True, max_length=40)),
                ("business_email", models.EmailField(blank=True, max_length=254)),
                ("business_address", models.CharField(blank=True, max_length=255)),
                (
                    "currency_symbol",
                    models.CharField(
                        blank=True,
                        help_text="Shown before every amount, e.g. ETB.",
                        max_length=8,
                    ),
                ),
                (
                    "default_credit_due_days",
                    models.PositiveSmallIntegerField(
                        default=30,
                        help_text="How long a customer has to settle a credit sale, "
                        "by default.",
                    ),
                ),
                (
                    "low_stock_threshold",
                    models.PositiveSmallIntegerField(
                        default=5,
                        help_text="Suggested reorder level for a new product.",
                    ),
                ),
                (
                    "allow_self_registration",
                    models.BooleanField(
                        default=True,
                        help_text=(
                            "Let new staff sign themselves up with a role passcode. "
                            "Turning this off means accounts can only be created from "
                            "Users & Roles."
                        ),
                    ),
                ),
                (
                    "require_credit_approval",
                    models.BooleanField(
                        default=True,
                        help_text=(
                            "Only let a customer buy on credit once someone has "
                            "explicitly approved them for it. Turning this off lets "
                            "any registered customer run a balance, which is faster "
                            "and riskier."
                        ),
                    ),
                ),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "updated_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="+",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "verbose_name": "System settings",
                "verbose_name_plural": "System settings",
            },
        ),
    ]
