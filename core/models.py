"""Abstract base models shared across every app."""
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
