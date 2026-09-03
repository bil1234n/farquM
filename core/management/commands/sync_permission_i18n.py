"""
Copy the permission catalogue into the browser's translation dictionaries.

WHY THIS EXISTS
---------------
The catalogue is Python (core/permissions.py, with Amharic in
core/permissions_am.py) because the API has to serve it and the phone renders
it directly. The web app translates itself in the browser instead, by matching
whole text nodes against static/i18n/<lang>.json after the page loads.

Those are two copies of the same sentences, and two copies drift. So the JSON
is generated from the Python rather than typed twice: add a permission, run

    python manage.py sync_permission_i18n

and the checkbox is translated on both platforms.

WHAT IT TOUCHES
---------------
Only keys of the shape `perm.*` and `group.*`. Everything else in those files
was written by hand for a specific screen and is left exactly as it is - this
command must be safe to run on a whim, or nobody will run it.

`--check` writes nothing and exits non-zero when something is out of date,
which is the form to put in CI.
"""
import json
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand

from core.permissions import translation_pairs
from core.permissions_am import TRANSLATIONS

#: The language the catalogue is authored in. Its file gets the source text.
SOURCE_LANG = "en"


def dictionary_path(lang: str) -> Path:
    """Where a language's dictionary lives, whatever STATICFILES_DIRS says."""
    for directory in getattr(settings, "STATICFILES_DIRS", []) or []:
        candidate = Path(directory) / "i18n" / f"{lang}.json"
        if candidate.exists():
            return candidate
    return Path(settings.BASE_DIR) / "static" / "i18n" / f"{lang}.json"


class Command(BaseCommand):
    help = "Write the permission catalogue into static/i18n/*.json."

    def add_arguments(self, parser):
        parser.add_argument(
            "--check",
            action="store_true",
            help="Report what would change and exit 1, writing nothing.",
        )

    def handle(self, *args, **options):
        check = options["check"]
        pairs = translation_pairs()
        stale = 0

        languages = [SOURCE_LANG, *TRANSLATIONS.keys()]
        for lang in languages:
            path = dictionary_path(lang)
            if not path.exists():
                self.stderr.write(f"skipping {lang}: {path} does not exist")
                continue

            data = json.loads(path.read_text(encoding="utf-8"))
            table = TRANSLATIONS.get(lang, {})
            changed = []

            for key, english in pairs:
                # The source language gets the English; every other language
                # gets its translation, or the English as a visible stand-in
                # for one that has not been written yet. A missing translation
                # must never render as a blank checkbox.
                wanted = english if lang == SOURCE_LANG else table.get(key) or english
                if data.get(key) != wanted:
                    changed.append(key)
                    data[key] = wanted

            if not changed:
                self.stdout.write(f"{lang}: up to date ({len(pairs)} entries)")
                continue

            stale += len(changed)
            if check:
                self.stdout.write(f"{lang}: {len(changed)} entry/entries out of date")
                for key in changed[:10]:
                    self.stdout.write(f"    {key}")
                continue

            # indent + ensure_ascii=False so the file stays readable and
            # reviewable in a diff, which is the whole point of checking a
            # translation file into version control.
            path.write_text(
                json.dumps(data, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            self.stdout.write(
                self.style.SUCCESS(f"{lang}: wrote {len(changed)} entry/entries")
            )

        if check and stale:
            # Non-zero so CI notices. The message says the fix, because an
            # exit code on its own teaches nobody anything.
            raise SystemExit(
                "Translation dictionaries are out of date. "
                "Run: python manage.py sync_permission_i18n"
            )

        missing = [
            key
            for lang, table in TRANSLATIONS.items()
            for key, _english in pairs
            if key not in table
        ]
        if missing:
            self.stdout.write(
                self.style.WARNING(
                    f"{len(missing)} catalogue string(s) still have no translation "
                    f"and will show in English: {', '.join(missing[:6])}"
                    + (" ..." if len(missing) > 6 else "")
                )
            )
