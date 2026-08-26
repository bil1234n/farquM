"""
Device registry and notification history for the mobile app.

A DeviceToken is the address Firebase uses to reach one installation of the
app. They are per-install, not per-user: one person with a phone and a tablet
has two rows, and both are notified. A token dies when the app is uninstalled,
at which point Firebase reports it as unregistered and we deactivate it.
"""
from django.conf import settings
from django.db import models
from django.utils import timezone

from core.models import TimeStampedModel


class DeviceToken(TimeStampedModel):
    class Platform(models.TextChoices):
        ANDROID = "ANDROID", "Android"
        IOS = "IOS", "iOS"
        WEB = "WEB", "Web"

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="devices"
    )
    token = models.CharField(max_length=255, unique=True, db_index=True)
    platform = models.CharField(
        max_length=10, choices=Platform.choices, default=Platform.ANDROID
    )
    device_name = models.CharField(max_length=120, blank=True)
    app_version = models.CharField(max_length=30, blank=True)

    is_active = models.BooleanField(default=True, db_index=True)
    last_seen = models.DateTimeField(default=timezone.now)
    deactivated_reason = models.CharField(max_length=120, blank=True)

    class Meta:
        ordering = ["-last_seen"]
        indexes = [models.Index(fields=["user", "is_active"])]

    def __str__(self):
        return f"{self.user.username} / {self.get_platform_display()} / {self.token[:16]}..."

    def touch(self):
        self.last_seen = timezone.now()
        self.save(update_fields=["last_seen"])

    def deactivate(self, reason=""):
        self.is_active = False
        self.deactivated_reason = reason[:120]
        self.save(update_fields=["is_active", "deactivated_reason"])


class NotificationLog(TimeStampedModel):
    """
    What we sent, to whom, and whether it landed.

    Kept because "I never got told the stock was out" is a common dispute and
    a log settles it. Also makes it obvious when FCM is quietly failing.
    """

    class Channel(models.TextChoices):
        STOCK = "stock", "Stock"
        CREDIT = "credit", "Credit"
        SALES = "sales", "Sales"
        GENERAL = "general", "General"

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
        related_name="notifications", null=True, blank=True,
    )
    title = models.CharField(max_length=160)
    body = models.TextField()
    channel = models.CharField(max_length=10, choices=Channel.choices, default=Channel.GENERAL)
    data = models.JSONField(null=True, blank=True)

    devices_targeted = models.PositiveIntegerField(default=0)
    devices_delivered = models.PositiveIntegerField(default=0)
    was_sent = models.BooleanField(default=False)
    error = models.TextField(blank=True)

    read_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["user", "-created_at"]),
            models.Index(fields=["channel", "-created_at"]),
        ]

    def __str__(self):
        return f"{self.title} -> {self.user_id or 'broadcast'}"

    @property
    def is_read(self) -> bool:
        return self.read_at is not None

    def mark_read(self):
        if not self.read_at:
            self.read_at = timezone.now()
            self.save(update_fields=["read_at"])
