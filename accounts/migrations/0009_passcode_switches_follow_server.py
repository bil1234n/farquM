"""
Stop old "switched off" records from blocking a registration passcode set on
the server.

THE PROBLEM
-----------
Saving Settings -> Security (or the same screen in the app) stored every
role's switch, including roles that had no passcode at all - and those can
only ever be stored as OFF, because the screen refuses to open a role without
a code. That OFF then counted as an administrator's decision, and a decision
wins over the server's codes. So a code added to the server later - Sales,
Stock keeper - was ignored, and the registration form kept offering only the
roles that had a code when the screen was last saved.

From now on such a save records no decision (see core.views and
api.parity_views). This migration clears the ones already stored: a row that
is switched off with no passcode of its own goes back to following the server
- unless the audit log shows somebody actually turned it off, which is the
one case where OFF was a real choice and must stand.

Data only, and conservative: when in doubt a row stays as it is, and a row
that stays as it is keeps its door shut.
"""
from django.db import migrations


def forwards(apps, schema_editor):
    RegistrationPasscode = apps.get_model("accounts", "RegistrationPasscode")
    RoleDefinition = apps.get_model("accounts", "RoleDefinition")
    AuditLog = apps.get_model("accounts", "AuditLog")

    names = dict(RoleDefinition.objects.values_list("code", "name"))
    stuck = RegistrationPasscode.objects.filter(
        passcode_hash="", is_enabled=False, updated_by__isnull=False
    )
    for row in stuck:
        name = names.get(row.role_code) or row.role_code.replace("_", " ").title()
        # The wording both screens write when a person flips a switch off.
        turned_off = AuditLog.objects.filter(
            description__contains=f"{name} registration turned off"
        ).exists()
        if turned_off:
            continue
        row.updated_by = None
        row.save(update_fields=["updated_by"])


class Migration(migrations.Migration):

    dependencies = [
        ("accounts", "0008_note_tag"),
    ]

    operations = [
        # Nothing to undo: going back simply leaves the rows following the
        # server, which the older code reads as switched off anyway.
        migrations.RunPython(forwards, migrations.RunPython.noop),
    ]
