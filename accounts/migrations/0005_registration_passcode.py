"""
Move registration passcodes out of the .env file and into the database.

WHAT THIS FIXES
---------------
Before this, `available_roles()` offered a role only if an environment
variable held a passcode for it. `PASSCODE_SALES` had never been set, so the
Sales role was simply absent from the registration form with nothing on the
screen to say why. Adding it meant editing a file on the server.

After this, every built-in role has a passcode row an administrator can set
from Settings -> Security, and the environment variable remains a fallback so
existing deployments keep working unchanged on the day this ships.

A row is created for each system role. `is_enabled` starts as True only where
an environment variable already supplies a code - a role nobody configured
must not become open to strangers just because this migration ran.
"""
import django.db.models.deletion
from django.conf import settings as django_settings
from django.db import migrations, models


def seed(apps, schema_editor):
    RegistrationPasscode = apps.get_model("accounts", "RegistrationPasscode")
    RoleDefinition = apps.get_model("accounts", "RoleDefinition")

    env_codes = {
        "ADMIN": getattr(django_settings, "REGISTRATION_PASSCODE_ADMIN", "") or "",
        "MANAGER": getattr(django_settings, "REGISTRATION_PASSCODE_MANAGER", "") or "",
        "SALES": getattr(django_settings, "REGISTRATION_PASSCODE_SALES", "") or "",
    }

    for code in ("ADMIN", "MANAGER", "SALES"):
        if not RoleDefinition.objects.filter(code=code).exists():
            continue
        RegistrationPasscode.objects.get_or_create(
            role_code=code,
            defaults={
                # The hash stays blank: the environment variable is still the
                # source of truth until an administrator types a new code.
                # Copying it in here would silently freeze the value, so
                # rotating the .env would stop having any effect.
                "passcode_hash": "",
                "is_enabled": bool(env_codes.get(code)),
                "note": (
                    "Set from the server environment."
                    if env_codes.get(code)
                    else ""
                ),
            },
        )


def unseed(apps, schema_editor):
    apps.get_model("accounts", "RegistrationPasscode").objects.all().delete()


class Migration(migrations.Migration):

    dependencies = [
        ("accounts", "0004_seed_roles"),
    ]

    operations = [
        migrations.CreateModel(
            name="RegistrationPasscode",
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
                    "role_code",
                    models.CharField(
                        help_text="The RoleDefinition.code this passcode registers for.",
                        max_length=32,
                        unique=True,
                    ),
                ),
                (
                    "passcode_hash",
                    models.CharField(
                        blank=True,
                        help_text="Hashed. Blank means no passcode has been set from the app.",
                        max_length=255,
                    ),
                ),
                (
                    "is_enabled",
                    models.BooleanField(
                        default=False,
                        help_text=(
                            "Off means nobody may register as this role, even if a "
                            "passcode is set. Turning it off is the quick way to close "
                            "a door without losing the code."
                        ),
                    ),
                ),
                (
                    "note",
                    models.CharField(
                        blank=True,
                        help_text=(
                            "A reminder for administrators, e.g. 'given to shop staff "
                            "on 1 Jan'. Never shown to the person registering."
                        ),
                        max_length=120,
                    ),
                ),
                ("last_used_at", models.DateTimeField(blank=True, null=True)),
                ("use_count", models.PositiveIntegerField(default=0)),
                (
                    "updated_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="+",
                        to="accounts.user",
                    ),
                ),
            ],
            options={
                "verbose_name": "Registration passcode",
                "verbose_name_plural": "Registration passcodes",
                "ordering": ["role_code"],
            },
        ),
        migrations.RunPython(seed, unseed),
    ]
