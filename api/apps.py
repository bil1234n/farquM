from django.apps import AppConfig


class ApiConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "api"
    verbose_name = "Mobile API & Notifications"

    def ready(self):
        from . import signals  # noqa: F401
