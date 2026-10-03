"""
Give the Manager role the Audit.

Data only, the same pattern as 0006 and 0007: accounts/roles.py is the
shipped default and ensure_system_roles() never overwrites a role that
already exists, so on a database that already ran, the new code would sit in
the catalogue granted to nobody but the owner. This adds `costing.view` to the
existing Manager row and touches nothing else. `costing.set` - saying what a
product really costs - is not handed out: that stays the owner's call.
"""
from django.db import migrations

GRANTS = {"MANAGER": ["costing.view"]}


def grant(apps, schema_editor):
    RoleDefinition = apps.get_model("accounts", "RoleDefinition")
    for code, additions in GRANTS.items():
        role = RoleDefinition.objects.filter(code=code).first()
        if role is None:
            continue
        held = list(role.permissions or [])
        if "*" in held:
            continue
        missing = [c for c in additions if c not in held]
        if missing:
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
        ("accounts", "0009_passcode_switches_follow_server"),
    ]

    operations = [
        migrations.RunPython(grant, revoke),
    ]
