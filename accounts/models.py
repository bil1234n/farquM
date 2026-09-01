"""
Custom user model, roles, and the audit trail.

ACCESS IS TWO INDEPENDENT QUESTIONS
-----------------------------------
    WHAT may this person do?    -> permissions (core/permissions.py)
    WHOSE records may they see? -> data scope   (core/scoping.py)

They are deliberately separate. "May record a repayment" and "may see the
whole shop's debts" are different grants, and collapsing them into one role
string is exactly what made the old two-role system unable to express a
sales assistant.

    RoleDefinition  a named bundle of permission codes + a default scope.
                    ADMIN / MANAGER / SALES ship as system roles and cannot be
                    deleted; an administrator may create as many more as they
                    like (Cashier, Stock Clerk, Auditor...).

    User.role       the role's code. Kept as a plain string rather than a
                    ForeignKey because it is read on nearly every request and
                    written into audit rows, migrations and the mobile cache -
                    a string survives a role being renamed or removed, an
                    integer FK does not.

    User.extra_permissions / denied_permissions
                    per-person adjustments on top of the role. This is what
                    "the admin gives them the exact access they have" means:
                    two sales users can share a role and still differ by one
                    checkbox, without inventing a role for each of them.

    effective = (role.permissions | extra) - denied

Deny wins over grant. If a code appears in both lists the user does not have
it - the safer reading of a contradiction, and the one an administrator
expects when they untick a box the role had ticked.
"""
from django.contrib.auth.models import AbstractUser, UserManager as DjangoUserManager
from django.db import models
from django.urls import reverse
from django.utils import timezone
from django.utils.functional import cached_property

from core.models import TimeStampedModel
from core.permissions import WILDCARD, clean_codes, expand
from core.utils import avatar_upload_path, validate_avatar_file


class RoleCode(models.TextChoices):
    """
    The three roles that always exist.

    Custom roles are rows in RoleDefinition with codes not listed here. This
    enum exists so code that genuinely means "the administrator role" can say
    so (`RoleCode.ADMIN`) instead of repeating a string literal.
    """

    ADMIN = "ADMIN", "Administrator"
    MANAGER = "MANAGER", "Manager"
    SALES = "SALES", "Sales"


#: Historical alias. Plenty of modules do `from .models import Role`.
Role = RoleCode

SYSTEM_ROLE_CODES = frozenset(RoleCode.values)


class DataScope(models.TextChoices):
    """Whose records a user may see. Enforced in core/scoping.py."""

    OWN = "OWN", "Own records only"
    TEAM = "TEAM", "Own records and their team's"
    ALL = "ALL", "Everything in the business"


class RoleDefinitionQuerySet(models.QuerySet):
    def active(self):
        return self.filter(is_active=True)

    def assignable(self):
        return self.active().order_by("rank", "name")

    def as_choices(self):
        return [(r.code, r.name) for r in self.assignable()]


class RoleDefinition(TimeStampedModel):
    """
    A named bundle of permissions.

    `permissions` is a JSON list of codes from core.permissions. The ADMIN
    role stores the single wildcard `"*"`, which means "everything, including
    permissions that do not exist yet". That is the correct behaviour for the
    owner and the wrong behaviour for anybody else, so no other role should
    ever be given it.
    """

    code = models.CharField(
        max_length=32,
        unique=True,
        help_text="Short uppercase identifier, e.g. CASHIER. Never changes.",
    )
    name = models.CharField(max_length=60)
    description = models.TextField(
        blank=True,
        help_text="Shown to administrators when picking a role for someone.",
    )
    permissions = models.JSONField(
        default=list,
        blank=True,
        help_text='Permission codes from the catalogue, or ["*"] for full access.',
    )
    data_scope = models.CharField(
        max_length=8,
        choices=DataScope.choices,
        default=DataScope.OWN,
        help_text="Whose records this role may see.",
    )
    is_system = models.BooleanField(
        default=False,
        help_text="Built-in role. Its code and system status cannot be changed, "
                  "and it cannot be deleted.",
    )
    is_active = models.BooleanField(default=True)
    rank = models.PositiveSmallIntegerField(
        default=50,
        help_text="Sort order. Lower is more senior - 10 Admin, 20 Manager, "
                  "30 Sales.",
    )

    objects = RoleDefinitionQuerySet.as_manager()

    class Meta:
        ordering = ["rank", "name"]
        verbose_name = "Role"
        verbose_name_plural = "Roles"

    def __str__(self):
        return self.name

    def save(self, *args, **kwargs):
        self.code = (self.code or "").strip().upper().replace(" ", "_")
        if self.is_full_access:
            self.permissions = [WILDCARD]
        else:
            self.permissions = clean_codes(self.permissions)
        super().save(*args, **kwargs)

    @property
    def is_full_access(self) -> bool:
        return WILDCARD in (self.permissions or [])

    @cached_property
    def permission_set(self) -> frozenset[str]:
        return expand(self.permissions)

    @property
    def permission_count(self) -> int:
        return len(self.permission_set)

    @property
    def user_count(self) -> int:
        # User.role is a code string, not a ForeignKey, so there is no reverse
        # accessor to count through - see the module docstring for why.
        return User.objects.filter(role=self.code).count()

    @property
    def can_be_deleted(self) -> bool:
        return not self.is_system and self.user_count == 0

    def get_absolute_url(self):
        return reverse("core:role_update", args=[self.pk])


class UserQuerySet(models.QuerySet):
    def admins(self):
        return self.filter(role=RoleCode.ADMIN)

    def managers(self):
        return self.filter(role=RoleCode.MANAGER)

    def sales(self):
        return self.filter(role=RoleCode.SALES)

    def active_staff(self):
        return self.filter(is_active=True)


class UserManager(DjangoUserManager.from_queryset(UserQuerySet)):
    def create_superuser(self, username, email=None, password=None, **extra):
        extra.setdefault("role", RoleCode.ADMIN)
        return super().create_superuser(username, email, password, **extra)


class User(AbstractUser):
    # Deliberately NOT a ForeignKey and deliberately without `choices`:
    # see the module docstring. get_role_display() is defined below, so
    # templates and audit messages keep working unchanged.
    role = models.CharField(
        max_length=32,
        default=RoleCode.MANAGER,
        db_index=True,
        help_text="Which role's permissions this user starts from.",
    )
    manager = models.ForeignKey(
        "self",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="team",
        help_text=(
            "For a sales user: whose stock they sell. They see that person's "
            "products, while their own sales, customers and debts stay theirs."
        ),
    )
    data_scope_override = models.CharField(
        max_length=8,
        choices=DataScope.choices,
        blank=True,
        default="",
        help_text="Leave blank to use the role's scope.",
    )
    extra_permissions = models.JSONField(
        default=list,
        blank=True,
        help_text="Granted to this person on top of their role.",
    )
    denied_permissions = models.JSONField(
        default=list,
        blank=True,
        help_text="Taken away from this person even though their role has it.",
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

    # -- Role ----------------------------------------------------------------
    @cached_property
    def role_definition(self):
        """
        The RoleDefinition row for this user's role code, or None.

        None is survivable: a role deleted out from under a user leaves them
        with no permissions at all rather than with everything, which is the
        direction a failure here has to lean.
        """
        if not self.role:
            return None
        return RoleDefinition.objects.filter(code=self.role).first()

    def get_role_display(self) -> str:
        role = self.role_definition
        if role:
            return role.name
        return (self.role or "").replace("_", " ").title() or "No role"

    # -- Permissions ---------------------------------------------------------
    @cached_property
    def effective_permissions(self) -> frozenset[str]:
        """
        Everything this user may do: role, plus their grants, minus their
        denials. Computed once per request-object, not per check.

        A Django superuser always gets everything. That is not a convenience -
        it is the recovery path. Someone locked out by a bad permission edit
        has to be able to get back in from the server, and `createsuperuser`
        is the only door that does not itself require a permission.
        """
        if self.is_superuser:
            from core.permissions import ALL_CODES

            return ALL_CODES

        if not self.is_active:
            return frozenset()

        role = self.role_definition
        granted = set(role.permission_set) if role else set()
        granted |= expand(self.extra_permissions)
        # Deny after grant: an explicit revocation must beat both the role and
        # any extra, otherwise "untick this one box" silently does nothing.
        granted -= expand(self.denied_permissions)
        return frozenset(granted)

    def has_access(self, *codes: str, require_all: bool = True) -> bool:
        """
        Permission check used everywhere.

        Named `has_access` rather than `has_perm` on purpose: `has_perm` is
        Django's own method for auth.Permission rows and is still used by the
        Django admin site. Overloading it would make two different systems
        answer to the same call.
        """
        if not codes:
            return True
        if not (self.is_authenticated and self.is_active):
            return False
        held = self.effective_permissions
        check = all if require_all else any
        return check(code in held for code in codes)

    def has_any_access(self, *codes: str) -> bool:
        return self.has_access(*codes, require_all=False)

    def refresh_access(self):
        """Drop the cached permission set after an edit within one request."""
        for attr in ("effective_permissions", "role_definition", "data_scope"):
            self.__dict__.pop(attr, None)

    @cached_property
    def data_scope(self) -> str:
        if self.is_superuser:
            return DataScope.ALL
        if self.data_scope_override:
            return self.data_scope_override
        role = self.role_definition
        return role.data_scope if role else DataScope.OWN

    # -- Convenience flags used across templates, views and the API ----------
    @property
    def is_admin(self) -> bool:
        """
        Full control of the system.

        Defined by permission rather than by role string, so a custom role
        granted the wildcard behaves like an administrator and a user whose
        ADMIN role was stripped of everything does not.
        """
        return self.is_superuser or self.has_access("user.permissions")

    @property
    def is_manager(self) -> bool:
        return self.role == RoleCode.MANAGER

    @property
    def is_sales(self) -> bool:
        return self.role == RoleCode.SALES

    @property
    def can_view_costs(self) -> bool:
        """Cost prices and stock valuation."""
        return self.has_access("product.view_cost")

    @property
    def can_view_profit(self) -> bool:
        """Gross profit, margins, cost of goods sold."""
        return self.has_access("report.profit")

    @property
    def can_view_financials(self) -> bool:
        """
        Legacy name, kept because it is read in several templates and in the
        API serializers. It has always meant "may see cost figures".
        """
        return self.can_view_costs

    @property
    def can_manage_users(self) -> bool:
        return self.has_access("user.view")

    @property
    def can_delete_records(self) -> bool:
        """Holds at least one of the destructive overrides."""
        return self.has_any_access(
            "sale.void",
            "credit.write_off",
            "credit.reverse_payment",
            "sale.receipt.delete",
        )

    @property
    def can_change_settings(self) -> bool:
        return self.has_access("settings.edit")

    @property
    def sees_all_data(self) -> bool:
        return self.data_scope == DataScope.ALL

    # -- Team ----------------------------------------------------------------
    @cached_property
    def team_ids(self) -> frozenset[int]:
        """Primary keys of the users who report to this one."""
        if not self.pk:
            return frozenset()
        return frozenset(
            User.objects.filter(manager_id=self.pk).values_list("pk", flat=True)
        )

    @property
    def scope_label(self) -> str:
        return {
            DataScope.ALL: "Every record in the business",
            DataScope.TEAM: "Own records and their team's",
            DataScope.OWN: "Own records only",
        }.get(self.data_scope, "Own records only")

    # -- Display -------------------------------------------------------------
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
    ACCESS = "ACCESS", "Access changed"


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
