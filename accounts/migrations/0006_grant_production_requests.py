"""
Hand the two new production-request permissions to the roles that need them.

WHY A MIGRATION AND NOT JUST A BLUEPRINT EDIT
---------------------------------------------
accounts/roles.py is the shipped default, and `ensure_system_roles()`
deliberately never overwrites a role that already exists - an administrator's
own tuning must survive a deploy. So on every database that already ran, the
new codes would exist in the catalogue and be granted to nobody: the "Ask for
production" button would be invisible to the very people it was built for, and
nothing would say why.

This adds the codes to the existing rows and touches nothing else. A role that
somebody has since customised keeps every other change they made, and a role
they have already granted these to is left alone.
"""
from django.db import migrations

#: role code -> codes to add
GRANTS = {
    "SALES": ["production.request"],
    "MANAGER": ["production.request", "production.approve"],
}


def grant(apps, schema_editor):
    RoleDefinition = apps.get_model("accounts", "RoleDefinition")

    for code, additions in GRANTS.items():
        role = RoleDefinition.objects.filter(code=code).first()
        if role is None:
            continue
        held = list(role.permissions or [])
        # The wildcard already covers everything, now and in future. Appending
        # to it would turn "full access" into a fixed list, which is the one
        # edit that must never happen to the ADMIN-shaped role.
        if "*" in held:
            continue
        missing = [c for c in additions if c not in held]
        if not missing:
            continue
        role.permissions = held + missing
        role.save(update_fields=["permissions"])


def revoke(apps, schema_editor):
    RoleDefinition = apps.get_model("accounts", "RoleDefinition")

    for code, additions in GRANTS.items():
        role = RoleDefinition.objects.filter(code=code).first()
        if role is None or "*" in (role.permissions or []):
            continue
        role.permissions = [c for c in (role.permissions or []) if c not in additions]
        role.save(update_fields=["permissions"])


class Migration(migrations.Migration):

    dependencies = [
        ("accounts", "0005_registration_passcode"),
    ]

    operations = [
        migrations.RunPython(grant, revoke),
    ]
