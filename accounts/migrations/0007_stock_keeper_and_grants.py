"""
The stock keeper role, and the new delivery and expense codes for the roles
that already exist.

Data only. The pattern is accounts/0006's: accounts/roles.py is the shipped
default, and `ensure_system_roles()` never overwrites a role that already
exists, so on every database that already ran, new codes would sit in the
catalogue granted to nobody. This adds them to the existing rows and touches
nothing else - a role somebody has since customised keeps every other change
they made.
"""
from django.db import migrations

#: role code -> codes to add
GRANTS = {
    "MANAGER": [
        "delivery.view",
        "delivery.record",
        "expense.view",
        "expense.record",
        "expense.void",
        "employee.view",
        "employee.manage",
    ],
    "SALES": ["delivery.view"],
}


def forwards(apps, schema_editor):
    from accounts.roles import ensure_system_roles

    RoleDefinition = apps.get_model("accounts", "RoleDefinition")

    # Creates STOCK_KEEPER from its blueprint. The three older roles already
    # exist and are left exactly as they are.
    ensure_system_roles(role_model=RoleDefinition)

    for code, additions in GRANTS.items():
        role = RoleDefinition.objects.filter(code=code).first()
        if role is None:
            continue
        held = list(role.permissions or [])
        # The wildcard already covers everything, now and in future. Appending
        # to it would turn "full access" into a fixed list.
        if "*" in held:
            continue
        missing = [c for c in additions if c not in held]
        if missing:
            role.permissions = held + missing
            role.save(update_fields=["permissions"])


def backwards(apps, schema_editor):
    RoleDefinition = apps.get_model("accounts", "RoleDefinition")
    User = apps.get_model("accounts", "User")

    for code, additions in GRANTS.items():
        role = RoleDefinition.objects.filter(code=code).first()
        if role is None or "*" in (role.permissions or []):
            continue
        role.permissions = [c for c in (role.permissions or []) if c not in additions]
        role.save(update_fields=["permissions"])

    # Only an unused role goes: deleting one that people hold would leave them
    # pointing at nothing.
    if not User.objects.filter(role="STOCK_KEEPER").exists():
        RoleDefinition.objects.filter(code="STOCK_KEEPER").delete()


class Migration(migrations.Migration):

    dependencies = [
        ("accounts", "0006_grant_production_requests"),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]
