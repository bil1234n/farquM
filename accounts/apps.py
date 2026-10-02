from django.apps import AppConfig
from django.db.models.signals import post_migrate


class AccountsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "accounts"
    verbose_name = "Users & Access Control"

    def ready(self):
        # After `manage.py flush` (and every migrate), put back any built-in
        # role the database has lost - see accounts.roles.reinstall_after_flush.
        from .roles import reinstall_after_flush

        post_migrate.connect(
            reinstall_after_flush,
            sender=self,
            dispatch_uid="accounts.reinstall_after_flush",
        )
