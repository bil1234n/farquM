"""
Every notification sent so far was about the business as it has run until
now - the MAIN branch. Data only (see core.tests.MigrationHygieneTests).
"""
from django.db import migrations


def into_main_branch(apps, schema_editor):
    db = schema_editor.connection.alias
    main = apps.get_model("core", "Branch").objects.using(db).get(is_main=True)
    apps.get_model("api", "NotificationLog").objects.using(db).filter(
        branch__isnull=True
    ).update(branch=main)


class Migration(migrations.Migration):

    dependencies = [
        ("api", "0002_notification_branch"),
        ("core", "0009_main_branch"),
    ]

    operations = [
        migrations.RunPython(into_main_branch, migrations.RunPython.noop),
    ]
