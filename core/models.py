"""Abstract base models shared across every app, plus system settings."""
from django.conf import settings
from django.db import models
from django.db.models.functions import Lower


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


class OptionQuerySet(models.QuerySet):
    def active(self):
        return self.filter(is_active=True)

    def in_group(self, key):
        return self.filter(group=(key or "").strip().upper())


#: group -> {code: label}, for the lists other tables store by code.
#:
#: A unit is read once per row on every product list, every sale line and
#: every material card - one query each would turn a single page into a
#: hundred. The table is a handful of short rows that change about twice a
#: year, so it is held in the process and dropped whenever an Option is
#: written. See Option.save().
_LABEL_CACHE: dict = {}


def labels_for(group: str) -> dict:
    """{code: label} for one coded group, cached."""
    group = (group or "").strip().upper()
    cached = _LABEL_CACHE.get(group)
    if cached is not None:
        return cached
    try:
        rows = dict(
            Option.objects.in_group(group)
            .exclude(code="")
            .values_list("code", "label")
        )
    except Exception:
        # The table may not exist yet - during the migration that creates it,
        # or on a half-built deployment. Falling back to the built-in wording
        # beats a 500 on the product list.
        return {}
    _LABEL_CACHE[group] = rows
    return rows


def forget_labels(group: str = "") -> None:
    """Drop the cached labels for one group, or for all of them."""
    if group:
        _LABEL_CACHE.pop((group or "").strip().upper(), None)
    else:
        _LABEL_CACHE.clear()


def coded_label(group: str, code: str, builtin=None) -> str:
    """
    What to show for a stored code.

    Order matters. The editable list wins, because renaming "Piece" to "Each"
    has to change what every product says. The built-in wording is the
    fallback for a deployment whose seed has not run, and the raw code is the
    last resort - a unit somebody deleted still has to render as something.
    """
    code = (code or "").strip()
    if not code:
        return ""
    found = labels_for(group).get(code)
    if found:
        return found
    if builtin:
        got = dict(builtin).get(code)
        if got:
            return str(got)
    return code.replace("_", " ").title()


class Option(TimeStampedModel):
    """
    One entry in a managed pick-list. The groups live in core/options.py.

    WHY ONE TABLE AND NOT EIGHT
    ---------------------------
    Every one of these lists behaves identically: show what exists, let
    somebody add the missing entry without leaving the form, let them take
    back a mistake. Eight tables would be eight of everything - migrations,
    endpoints, forms - to express one idea.

    WHY ROWS THAT USE AN OPTION ALSO STORE ITS NAME
    ----------------------------------------------
    Anything pointing here does so with on_delete=SET_NULL and keeps a
    snapshot of the label beside the link. Deleting 'Dashen Bank' a year from
    now must not quietly blank out what last March's sale said it was. The
    link is for grouping and filtering; the snapshot is the record.
    """

    group = models.CharField(
        max_length=40,
        db_index=True,
        help_text="Which list this belongs to. See core/options.py.",
    )
    label = models.CharField(max_length=120)
    #: Set only for groups whose CODE is written into another table's column -
    #: a unit, where every product row already holds "PIECE". Renaming such an
    #: entry to "Each" then leaves those rows pointing at the same thing,
    #: which is exactly what an editable unit list has to allow.
    code = models.CharField(
        max_length=32,
        blank=True,
        db_index=True,
        help_text="Stable identifier for lists other tables store by code.",
    )
    sort_order = models.PositiveSmallIntegerField(
        default=100, help_text="Lower sorts first. Ties fall back to the label."
    )
    is_active = models.BooleanField(
        default=True,
        db_index=True,
        help_text="Off hides it from new forms without touching old records.",
    )
    is_seeded = models.BooleanField(
        default=False,
        help_text="Shipped with the system rather than typed in by somebody.",
    )
    use_count = models.PositiveIntegerField(
        default=0,
        help_text="How often it has been picked. Drives 'most used first'.",
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="options_added",
    )

    objects = OptionQuerySet.as_manager()

    class Meta:
        ordering = ["sort_order", "label"]
        indexes = [
            models.Index(
                fields=["group", "is_active", "sort_order"],
                name="option_group_active_idx",
            )
        ]
        constraints = [
            models.UniqueConstraint(
                "group",
                Lower("label"),
                name="option_label_unique_per_group",
            )
        ]

    def __str__(self):
        return self.label

    def save(self, *args, **kwargs):
        from .options import is_coded

        self.group = (self.group or "").strip().upper()
        self.label = " ".join((self.label or "").split())[:120]
        self.code = (self.code or "").strip().upper()
        if not self.code and is_coded(self.group):
            self.code = self._generate_code()
        super().save(*args, **kwargs)
        # Units are read on every product row that renders. The cache is
        # cleared here rather than by a signal so it cannot be skipped by a
        # caller that saves through a different path.
        forget_labels(self.group)

    def delete(self, *args, **kwargs):
        group = self.group
        result = super().delete(*args, **kwargs)
        forget_labels(group)
        return result

    def _generate_code(self) -> str:
        """
        A stable identifier from the label: "Jerry can" -> JERRY_CAN.

        Kept short enough for the columns that store it, and made unique
        within the group so two similar names cannot collide into one unit.
        """
        base = "".join(
            ch if ch.isalnum() else "_" for ch in self.label.upper()
        ).strip("_")
        base = "_".join(part for part in base.split("_") if part)[:32] or "OPT"

        candidate, suffix = base, 1
        taken = Option.objects.in_group(self.group).exclude(pk=self.pk)
        while taken.filter(code=candidate).exists():
            suffix += 1
            candidate = f"{base[:29]}_{suffix}"
        return candidate

    @property
    def value(self) -> str:
        """What a client should send back: the code when there is one."""
        return self.code or str(self.pk)

    @property
    def group_label(self) -> str:
        from .options import group_for

        group = group_for(self.group)
        return group.label if group else self.group.replace("_", " ").title()

    def touch_use(self):
        """Count one use. Written with F() so two tills cannot lose a count."""
        Option.objects.filter(pk=self.pk).update(use_count=models.F("use_count") + 1)

    def may_be_removed_by(self, user) -> bool:
        """
        Who may take an entry back out of the list.

        Whoever typed it can undo their own typo - that is the whole point of
        letting them add one in the first place. Beyond that it is a shared
        list, so removing somebody else's entry (or one that shipped with the
        system) needs the permission that governs the other shared lookups.
        """
        if user is None or not getattr(user, "is_authenticated", False):
            return False
        if user.has_access("catalog.manage"):
            return True
        return bool(
            not self.is_seeded
            and self.created_by_id
            and self.created_by_id == user.pk
        )

    def may_be_renamed_by(self, user) -> bool:
        """
        Who may change the wording of an entry.

        Everyone who could remove it, plus one case removal does not allow:
        re-wording a seeded entry of a CODED list. "Piece" is wording, not
        identity - the products that use it store PIECE - so renaming it to
        "Each" re-words every product at once and loses nothing. That is the
        whole point of the units being editable, and it is why this is not
        simply may_be_removed_by: taking "Piece" OUT of the list would leave
        those products pointing at a unit nobody can read.

        For an uncoded list the rows that already used an entry keep their own
        snapshot of the label, so a rename only changes what future ones say.
        """
        if user is None or not getattr(user, "is_authenticated", False):
            return False
        if self.may_be_removed_by(user):
            return True
        return bool(self.code) and user.has_access("catalog.manage")


def resolve_option(group: str, *, label: str = "", option_id=None, user=None):
    """
    Turn what a form sent into (Option | None, label).

    A client may send an id it picked, a label it typed, or both. A label that
    matches nothing yet is CREATED - that is the "add it from inside the
    select" behaviour, and doing it here means every caller gets it without
    re-implementing the case-insensitive match that stops 'Dashen' and
    'dashen' becoming two banks.

    Returns (None, "") when nothing was chosen, which is a normal answer: a
    cash sale names no bank.
    """
    from .options import is_known

    group = (group or "").strip().upper()
    label = " ".join((label or "").split())[:120]

    if option_id:
        found = Option.objects.in_group(group).filter(pk=option_id).first()
        if found is not None:
            return found, found.label

    if not label or not is_known(group):
        return None, label

    existing = Option.objects.in_group(group).filter(label__iexact=label).first()
    if existing is not None:
        if not existing.is_active:
            Option.objects.filter(pk=existing.pk).update(is_active=True)
            existing.is_active = True
        return existing, existing.label

    created = Option.objects.create(
        group=group,
        label=label,
        created_by=user if getattr(user, "pk", None) else None,
    )
    return created, created.label
