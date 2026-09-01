"""Abstract base models shared across every app, plus system settings."""
from django.conf import settings
from django.db import models


class TimeStampedModel(models.Model):
    """Adds created_at / updated_at to any model."""

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


class AuthoredModel(TimeStampedModel):
    """Adds created_by / updated_by audit columns."""

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="%(app_label)s_%(class)s_created",
    )
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="%(app_label)s_%(class)s_updated",
    )

    class Meta:
        abstract = True


class OwnedModel(models.Model):
    """
    Adds the `owner` column that drives per-manager data isolation.

    A Manager may only ever see rows where owner == themselves. An Admin sees
    every row. The filtering itself lives in core.scoping - this class only
    provides the column, because a model that stores an owner but is never
    filtered is worse than one that has no owner at all: it looks safe.

    owner is nullable for two reasons only:
      1. rows that pre-date this feature, backfilled by migration;
      2. rows whose owning manager was later removed from the system.
    Both are treated as "Admin only" by core.scoping - never as "everyone" -
    so an unset owner can never leak data sideways.
    """

    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        db_index=True,
        related_name="%(app_label)s_%(class)s_owned",
        help_text=(
            "The staff member this record belongs to. Managers only ever see "
            "their own records; administrators see everyone's."
        ),
    )

    class Meta:
        abstract = True

    @property
    def owner_name(self) -> str:
        return self.owner.display_name if self.owner_id else "Unassigned"


class SoftDeleteQuerySet(models.QuerySet):
    def alive(self):
        return self.filter(is_deleted=False)

    def dead(self):
        return self.filter(is_deleted=True)


class SoftDeleteModel(models.Model):
    """
    Records are never physically removed - only Admin may soft-delete
    (see core.mixins.AdminRequiredMixin). This preserves the audit trail.
    """

    is_deleted = models.BooleanField(default=False, db_index=True)
    deleted_at = models.DateTimeField(null=True, blank=True)
    deleted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="%(app_label)s_%(class)s_deleted",
    )

    objects = SoftDeleteQuerySet.as_manager()

    class Meta:
        abstract = True

    def soft_delete(self, user=None):
        from django.utils import timezone

        self.is_deleted = True
        self.deleted_at = timezone.now()
        self.deleted_by = user
        self.save(update_fields=["is_deleted", "deleted_at", "deleted_by"])

    def restore(self):
        self.is_deleted = False
        self.deleted_at = None
        self.deleted_by = None
        self.save(update_fields=["is_deleted", "deleted_at", "deleted_by"])


class SystemSetting(models.Model):
    """
    Business configuration an administrator can change without a redeploy.

    Exactly one row ever exists - `pk` is pinned to 1 by `load()`. A singleton
    table rather than a key/value store because these values are read on every
    single page render through the context processor, and a typed column that
    the ORM can fetch in one query beats parsing strings out of a bag.

    Every field falls back to its `settings.py` value when blank, so a fresh
    deployment behaves exactly as it did before this table existed and an
    administrator can override piece by piece.
    """

    SINGLETON_PK = 1

    id = models.PositiveSmallIntegerField(primary_key=True, default=SINGLETON_PK)

    business_name = models.CharField(max_length=120, blank=True)
    business_phone = models.CharField(max_length=40, blank=True)
    business_email = models.EmailField(blank=True)
    business_address = models.CharField(max_length=255, blank=True)
    currency_symbol = models.CharField(
        max_length=8, blank=True, help_text="Shown before every amount, e.g. ETB."
    )

    default_credit_due_days = models.PositiveSmallIntegerField(
        default=30,
        help_text="How long a customer has to settle a credit sale, by default.",
    )
    low_stock_threshold = models.PositiveSmallIntegerField(
        default=5,
        help_text="Suggested reorder level for a new product.",
    )

    allow_self_registration = models.BooleanField(
        default=True,
        help_text=(
            "Let new staff sign themselves up with a role passcode. Turning "
            "this off means accounts can only be created from Users & Roles."
        ),
    )
    require_credit_approval = models.BooleanField(
        default=True,
        help_text=(
            "Only let a customer buy on credit once someone has explicitly "
            "approved them for it. Turning this off lets any registered "
            "customer run a balance, which is faster and riskier."
        ),
    )

    updated_at = models.DateTimeField(auto_now=True)
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )

    class Meta:
        verbose_name = "System settings"
        verbose_name_plural = "System settings"

    def __str__(self):
        return self.business_name or "System settings"

    def save(self, *args, **kwargs):
        # Pinning the pk is what makes this a singleton: a second save can
        # only ever be an update of row 1, never an insert of row 2.
        self.id = self.SINGLETON_PK
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise PermissionError("System settings cannot be deleted, only edited.")

    @classmethod
    def load(cls):
        """
        The settings row, creating it on first access.

        Never raises. This is called from a context processor on every page
        including the login page and the 500 handler, and an error here would
        turn a missing table during a half-finished migration into a site-wide
        outage with a confusing traceback.
        """
        try:
            obj, _ = cls.objects.get_or_create(pk=cls.SINGLETON_PK)
            return obj
        except Exception:
            return cls(pk=cls.SINGLETON_PK)

    # -- Resolved values: DB value if set, otherwise settings.py -------------
    def value(self, field: str, settings_key: str, default=""):
        stored = getattr(self, field, "") or ""
        if stored:
            return stored
        return getattr(settings, settings_key, default)

    @property
    def name(self) -> str:
        return self.value("business_name", "BUSINESS_NAME", "Business")

    @property
    def phone(self) -> str:
        return self.value("business_phone", "BUSINESS_PHONE")

    @property
    def address(self) -> str:
        return self.value("business_address", "BUSINESS_ADDRESS")

    @property
    def currency(self) -> str:
        return self.value("currency_symbol", "CURRENCY_SYMBOL", "ETB")
