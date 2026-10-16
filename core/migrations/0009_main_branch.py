"""
Branches, part 2 of 3: the MAIN branch, and every pick-list entry in it.

The main branch is the business as it has been until now. Everything that
already exists belongs to it; new branches start with lists of their own.
Data only - see core/0008 for why this is a migration of its own.
"""
from django.db import migrations

MAIN_NAME = "Main branch"


def create_main_branch(apps, schema_editor):
    Branch = apps.get_model("core", "Branch")
    rows = Branch.objects.using(schema_editor.connection.alias)
    if rows.filter(is_main=True).exists():
        return
    name, n = MAIN_NAME, 2
    while rows.filter(name=name).exists():
        name, n = f"{MAIN_NAME} {n}", n + 1
    rows.create(name=name, is_main=True)


def options_into_main(apps, schema_editor):
    db = schema_editor.connection.alias
    Branch = apps.get_model("core", "Branch")
    Option = apps.get_model("core", "Option")
    main = Branch.objects.using(db).get(is_main=True)
    Option.objects.using(db).filter(branch__isnull=True).update(branch=main)


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0008_branches"),
    ]

    operations = [
        migrations.RunPython(create_main_branch, migrations.RunPython.noop),
        migrations.RunPython(options_into_main, migrations.RunPython.noop),
    ]
