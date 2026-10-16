"""
File the existing activity log under the MAIN branch.

Everything logged so far happened in the business as it has run until now,
which is the main branch. The exception is what belongs to the whole
business - sign-ins, accounts, roles, settings - which stays blank and so
shows in every branch's log, as the same entries will from now on. Data only
(see core.tests.MigrationHygieneTests).
"""
from django.db import migrations

#: Entries about these are the business's, not one branch's.
BUSINESS_WIDE = (
    "",
    "User",
    "RoleDefinition",
    "RegistrationPasscode",
    "SystemSetting",
    "DeviceToken",
    "Branch",
)


def into_main_branch(apps, schema_editor):
    db = schema_editor.connection.alias
    main = apps.get_model("core", "Branch").objects.using(db).get(is_main=True)
    apps.get_model("accounts", "AuditLog").objects.using(db).filter(
        branch__isnull=True
    ).exclude(model_name__in=BUSINESS_WIDE).update(branch=main)


class Migration(migrations.Migration):

    dependencies = [
        ("accounts", "0011_branches"),
        ("core", "0009_main_branch"),
    ]

    operations = [
        migrations.RunPython(into_main_branch, migrations.RunPython.noop),
    ]
