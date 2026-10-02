from django.apps import AppConfig
from django.db.models.signals import post_migrate
from django.utils.autoreload import autoreload_started


def watch_env_file(sender, **kwargs):
    """
    Make `manage.py runserver` restart when .env changes, as it does for code.

    Every value read from .env - the registration passcodes, DEBUG, the
    business name - is read once, when the server process starts. The
    development server restarts itself when a Python file changes, but it
    never looked at .env, so an edit there seemed to be ignored: passcodes
    added for Sales and Stock keeper, and the registration form still offering
    only Administrator and Manager until somebody thought to restart it.

    `sender` is the autoreloader. A directory glob rather than the file itself,
    so a .env created after the server started is picked up too. Production
    (Vercel, gunicorn) never runs the autoreloader, so this never runs there.
    """
    from django.conf import settings

    sender.watch_dir(settings.BASE_DIR, ".env")


class CoreConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "core"
    verbose_name = "Core"

    def ready(self):
        # After `manage.py flush` (and every migrate), refill the pick-lists
        # if the table is empty - see core.options.refill_after_flush.
        from .options import refill_after_flush

        post_migrate.connect(
            refill_after_flush,
            sender=self,
            dispatch_uid="core.refill_after_flush",
        )
        autoreload_started.connect(watch_env_file, dispatch_uid="core.watch_env_file")
