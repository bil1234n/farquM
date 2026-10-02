from django.apps import AppConfig
from django.db.models.signals import post_migrate


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
