"""
Branches, part 2 of 3 for inventory: everything that already exists belongs to
the MAIN branch - the business as it has run until now. Data only.
"""
from django.db import migrations


def into_main_branch(apps, schema_editor):
    db = schema_editor.connection.alias
    main = apps.get_model("core", "Branch").objects.using(db).get(is_main=True)
    apps.get_model("inventory", "Category").objects.using(db).filter(
        branch__isnull=True
    ).update(branch=main)
    apps.get_model("inventory", "Supplier").objects.using(db).filter(
        branch__isnull=True
    ).update(branch=main)
    apps.get_model("inventory", "Product").objects.using(db).filter(
        branch__isnull=True
    ).update(branch=main)


class Migration(migrations.Migration):

    dependencies = [
        ("inventory", "0009_branch_columns"),
        ("core", "0009_main_branch"),
    ]

    operations = [
        migrations.RunPython(into_main_branch, migrations.RunPython.noop),
    ]
