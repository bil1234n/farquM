"""
Custom user model + audit trail.

The whole RBAC system hangs off User.role. There are deliberately only two
roles - adding a third means adding a constant here and updating
core.mixins plus the sidebar template. Nothing else.
"""
from django.contrib.auth.models import AbstractUser, UserManager as DjangoUserManager
from django.db import models
from django.urls import reverse
from django.utils import timezone

from core.models import TimeStampedModel
from core.utils import avatar_upload_path, validate_avatar_file


class Role(models.TextChoices):
    ADMIN = "ADMIN", "Administrator"
    MANAGER = "MANAGER", "Manager"


class UserQuerySet(models.QuerySet):
    def admins(self):
        return self.filter(role=Role.ADMIN)

    def managers(self):
        return self.filter(role=Role.MANAGER)

    def active_staff(self):
        return self.filter(is_active=True)


class UserManager(DjangoUserManager.from_queryset(UserQuerySet)):
    def create_superuser(self, username, email=None, password=None, **extra):
        extra.setdefault("role", Role.ADMIN)
        return super().create_superuser(username, email, password, **extra)


class User(AbstractUser):
    role = models.CharField(
        max_length=10,
        choices=Role.choices,
        default=Role.MANAGER,
        db_index=True,
        help_text="Administrator = full access. Manager = operational access only.",
    )
    phone = models.CharField(max_length=30, blank=True)
    employee_id = models.CharField(max_length=30, blank=True, unique=False)
    avatar = models.ImageField(
        upload_to=avatar_upload_path,
        blank=True,
        null=True,
        validators=[validate_avatar_file],
        help_text="Profile photo. Square images look best.",
    )
    last_activity = models.DateTimeField(null=True, blank=True)
    must_change_password = models.BooleanField(
        default=False,
        help_text="Force a password reset on next login (used after admin resets).",
    )
    notes = models.TextField(blank=True)

    objects = UserManager()

    class Meta:
        ordering = ["username"]
        verbose_name = "User"
        verbose_name_plural = "Users"
        indexes = [models.Index(fields=["role", "is_active"])]

    def __str__(self):
        return f"{self.get_full_name() or self.username} ({self.get_role_display()})"

    def get_absolute_url(self):
        return reverse("accounts:user_detail", args=[self.pk])

    # -- Role helpers used everywhere in templates and views -----------------
    @property
    def is_admin(self) -> bool:
        return self.role == Role.ADMIN

    @property
    def is_manager(self) -> bool:
        return self.role == Role.MANAGER

    @property
    def can_view_financials(self) -> bool:
        """Cost prices, profit margins, and P&L are Admin-only."""
        return self.is_admin

    @property
    def can_manage_users(self) -> bool:
        return self.is_admin

    @property
    def can_delete_records(self) -> bool:
        """Void / soft-delete / override is Admin-only."""
        return self.is_admin

    @property
    def can_change_settings(self) -> bool:
        return self.is_admin

    @property
    def display_name(self) -> str:
        return self.get_full_name() or self.username

    @property
    def initials(self) -> str:
        """Fallback avatar when no photo has been uploaded."""
        parts = (self.get_full_name() or self.username).split()
        if len(parts) >= 2:
            return (parts[0][:1] + parts[1][:1]).upper()
        return (parts[0][:2] if parts else "?").upper()

    @property
    def avatar_url(self) -> str | None:
        """
        Absolute-ish URL for the profile photo, or None.

        Wrapped in try/except because a storage backend can raise when the
        underlying file has gone - a real risk when switching between local
        disk and Cloudinary, since rows keep pointing at paths the new backend
        knows nothing about. A missing photo must never 500 a profile page.
        """
        if not self.avatar:
            return None
        try:
            return self.avatar.url
        except Exception:
            return None

    def touch(self):
        self.last_activity = timezone.now()
        self.save(update_fields=["last_activity"])


class AuditAction(models.TextChoices):
    CREATE = "CREATE", "Created"
    UPDATE = "UPDATE", "Updated"
    DELETE = "DELETE", "Deleted"
    VOID = "VOID", "Voided"
    LOGIN = "LOGIN", "Logged in"
    LOGOUT = "LOGOUT", "Logged out"
    LOGIN_FAILED = "LOGIN_FAILED", "Failed login"
    PAYMENT = "PAYMENT", "Payment recorded"
    STOCK = "STOCK", "Stock adjusted"
    OVERRIDE = "OVERRIDE", "Admin override"
    EXPORT = "EXPORT", "Data exported"


class AuditLog(TimeStampedModel):
    """
    Append-only record of who did what. Never updated, never deleted.
    Written by accounts.services.log_action().
    """

    user = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="audit_logs",
    )
    username_snapshot = models.CharField(
        max_length=150,
        blank=True,
        help_text="Kept so the log survives user deletion.",
    )
    action = models.CharField(max_length=20, choices=AuditAction.choices, db_index=True)
    model_name = models.CharField(max_length=100, blank=True, db_index=True)
    object_id = models.CharField(max_length=50, blank=True, db_index=True)
    object_repr = models.CharField(max_length=255, blank=True)
    description = models.TextField(blank=True)
    changes = models.JSONField(
        null=True, blank=True, help_text='{"field": {"from": x, "to": y}}'
    )
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.CharField(max_length=255, blank=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Audit log entry"
        verbose_name_plural = "Audit log"
        indexes = [
            models.Index(fields=["-created_at"]),
            models.Index(fields=["model_name", "object_id"]),
            models.Index(fields=["user", "-created_at"]),
        ]

    def __str__(self):
        who = self.user.username if self.user else self.username_snapshot or "system"
        return f"{who} {self.get_action_display()} {self.object_repr}".strip()

    def save(self, *args, **kwargs):
        if self.user and not self.username_snapshot:
            self.username_snapshot = self.user.username
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise PermissionError("Audit log entries are immutable and cannot be deleted.")
