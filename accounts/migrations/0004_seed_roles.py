"""
Install the three built-in roles and point existing accounts at them.

Existing ADMIN and MANAGER users keep their role code, so nobody's access
changes on the day this ships: the codes they already carry now resolve to
RoleDefinition rows holding the same permissions those roles always implied.

The reverse migration deletes only the system roles it created. Custom roles
an administrator added are left alone - a rollback should undo this change,
not somebody else's work.
"""
from django.db import migrations


def seed(apps, schema_editor):
    RoleDefinition = apps.get_model("accounts", "RoleDefinition")
    User = apps.get_model("accounts", "User")

    # Imported rather than duplicated so there is exactly one definition of
    # what "Manager" means. The blueprint module holds no model imports at
    # module level, which is what makes it safe to use from a migration.
    from accounts.roles import BLUEPRINTS

    for code, spec in BLUEPRINTS.items():
        RoleDefinition.objects.update_or_create(
            code=code,
            defaults={
                "name": spec["name"],
                "description": spec["description"],
                "permissions": list(spec["permissions"]),
                "data_scope": spec["data_scope"],
                "rank": spec["rank"],
                "is_system": True,
                "is_active": True,
            },
        )

    # Anything holding a role code that no longer resolves would end up with
    # no permissions at all. Nothing should match this today - the only codes
    # in the wild are ADMIN and MANAGER - but a blank or hand-edited value is
    # exactly the kind of row that turns into a silent lockout months later.
    known = set(BLUEPRINTS)
    User.objects.exclude(role__in=known).update(role="MANAGER")


def unseed(apps, schema_editor):
    RoleDefinition = apps.get_model("accounts", "RoleDefinition")
    RoleDefinition.objects.filter(is_system=True).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("accounts", "0003_roles_and_access"),
    ]

    operations = [
        migrations.RunPython(seed, unseed),
    ]
