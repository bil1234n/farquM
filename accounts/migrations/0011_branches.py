"""
Branches, for accounts.

    User.branches        the branches a person works in. Empty means the
                         main branch - so every account that exists today
                         keeps working exactly where it did.
    User.active_branch   the branch they last switched to.
    AuditLog.branch      which branch's log an entry belongs to. Blank for
                         what belongs to the whole business (a sign-in, an
                         account, a role); accounts/0012 files the existing
                         entries.
"""

import core.branching
import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("accounts", "0010_grant_costing_view"),
        ("core", "0010_option_branch_required"),
    ]

    operations = [
        migrations.AddField(
            model_name="auditlog",
            name="branch",
            field=models.ForeignKey(
                blank=True,
                default=core.branching.current_branch_id,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="+",
                to="core.branch",
            ),
        ),
        migrations.AddField(
            model_name="user",
            name="active_branch",
            field=models.ForeignKey(
                blank=True,
                help_text="The branch this person is working in now.",
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="+",
                to="core.branch",
            ),
        ),
        migrations.AddField(
            model_name="user",
            name="branches",
            field=models.ManyToManyField(
                blank=True,
                help_text="The branches this person works in. Leave empty for the main branch only.",
                related_name="staff",
                to="core.branch",
            ),
        ),
    ]
