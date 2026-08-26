"""
Accounts Receivable: credit accounts, debts and the repayment ledger.

THE CENTRAL RULE OF THIS MODULE
-------------------------------
Repayment rows are APPEND-ONLY. They are never edited and never deleted.
A mistake is corrected by posting a REVERSAL row, not by changing history.

Consequently every balance in the system is *derived*:

    DebtRecord.amount_repaid    = SUM(active repayments on that debt)
    DebtRecord.balance          = principal - amount_repaid
    CreditAccount.outstanding   = SUM(balance of that customer's open debts)

The stored columns are caches, refreshed by recalculate(). If a figure is
ever disputed, it can be rebuilt from source rows and defended line by line.
"""
import datetime as dt
from decimal import Decimal

from django.conf import settings
from django.core.validators import MinValueValidator
from django.db import models
from django.db.models import Q, Sum
from django.urls import reverse
from django.utils import timezone

from core.models import OwnedModel, TimeStampedModel
from core.utils import ZERO, money, receipt_upload_path, validate_receipt_file


# ---------------------------------------------------------------------------
# Credit account (one per customer)
# ---------------------------------------------------------------------------
class CreditAccountQuerySet(models.QuerySet):
    def in_debt(self):
        return self.filter(outstanding_balance__gt=0)

    def over_limit(self):
        return self.filter(
            credit_limit__gt=0, outstanding_balance__gt=models.F("credit_limit")
        )


class CreditAccount(TimeStampedModel):
    """The borrower profile. Auto-created with every Customer via a signal."""

    customer = models.OneToOneField(
        "sales.Customer", on_delete=models.CASCADE, related_name="credit_account"
    )
    credit_limit = models.DecimalField(
        max_digits=14, decimal_places=2, default=ZERO,
        validators=[MinValueValidator(Decimal("0"))],
        help_text="Maximum debt allowed. 0 means no limit enforced.",
    )
    default_terms_days = models.PositiveIntegerField(
        default=30, help_text="Default number of days until a new debt falls due."
    )

    # --- Cached aggregates, refreshed by recalculate() ---------------------
    total_credit_extended = models.DecimalField(max_digits=16, decimal_places=2, default=ZERO)
    total_repaid = models.DecimalField(max_digits=16, decimal_places=2, default=ZERO)
    outstanding_balance = models.DecimalField(
        max_digits=14, decimal_places=2, default=ZERO, db_index=True
    )
    last_purchase_date = models.DateField(null=True, blank=True)
    last_payment_date = models.DateField(null=True, blank=True)

    is_blocked = models.BooleanField(
        default=False, help_text="Block further credit sales to this customer."
    )
    block_reason = models.CharField(max_length=255, blank=True)
    notes = models.TextField(blank=True)

    objects = CreditAccountQuerySet.as_manager()

    class Meta:
        ordering = ["-outstanding_balance"]
        verbose_name = "Credit account"

    def __str__(self):
        return f"Credit account - {self.customer.name}"

    def get_absolute_url(self):
        return reverse("credit:borrower_detail", args=[self.customer_id])

    # -- Derived -------------------------------------------------------------
    def recalculate(self, commit: bool = True):
        debts = self.customer.debts.exclude(status=DebtStatus.CANCELLED)
        agg = debts.aggregate(
            principal=Sum("principal"), repaid=Sum("amount_repaid"), balance=Sum("balance")
        )
        self.total_credit_extended = money(agg["principal"] or ZERO)
        self.total_repaid = money(agg["repaid"] or ZERO)
        self.outstanding_balance = money(agg["balance"] or ZERO)

        last_debt = debts.order_by("-created_at").first()
        if last_debt:
            self.last_purchase_date = last_debt.created_at.date()

        last_pay = (
            Repayment.objects.filter(debt__customer=self.customer, is_reversed=False)
            .order_by("-paid_at").first()
        )
        if last_pay:
            self.last_payment_date = last_pay.paid_at.date()

        if commit:
            self.save(update_fields=[
                "total_credit_extended", "total_repaid", "outstanding_balance",
                "last_purchase_date", "last_payment_date", "updated_at",
            ])
        return self

    @property
    def available_credit(self) -> Decimal:
        if self.credit_limit <= ZERO:
            return ZERO
        return money(max(self.credit_limit - self.outstanding_balance, ZERO))

    @property
    def utilisation_percent(self) -> Decimal:
        if self.credit_limit <= ZERO:
            return ZERO
        return money(min(self.outstanding_balance / self.credit_limit * 100, Decimal("999")))

    @property
    def is_over_limit(self) -> bool:
        return self.credit_limit > ZERO and self.outstanding_balance > self.credit_limit

    @property
    def open_debts(self):
        return self.customer.debts.open_debts()

    @property
    def overdue_debts(self):
        return self.customer.debts.overdue()

    @property
    def overdue_amount(self) -> Decimal:
        return money(self.overdue_debts.aggregate(t=Sum("balance"))["t"] or ZERO)

    @property
    def has_overdue(self) -> bool:
        return self.overdue_debts.exists()

    @property
    def risk_level(self) -> str:
        """Simple traffic light used across the borrower dashboard."""
        if self.outstanding_balance <= ZERO:
            return "CLEAR"
        if self.is_blocked:
            return "BLOCKED"
        overdue = self.overdue_debts
        if overdue.filter(
            due_date__lt=timezone.localdate() - dt.timedelta(days=60)
        ).exists():
            return "CRITICAL"
        if overdue.exists() or self.is_over_limit:
            return "AT_RISK"
        return "CURRENT"

    @property
    def risk_class(self) -> str:
        return {
            "CLEAR": "success", "CURRENT": "info", "AT_RISK": "warning",
            "CRITICAL": "danger", "BLOCKED": "dark",
        }.get(self.risk_level, "secondary")

    @property
    def risk_label(self) -> str:
        return {
            "CLEAR": "No debt", "CURRENT": "Current", "AT_RISK": "At risk",
            "CRITICAL": "Critical", "BLOCKED": "Blocked",
        }.get(self.risk_level, self.risk_level)


# ---------------------------------------------------------------------------
# Debt records
# ---------------------------------------------------------------------------
class DebtStatus(models.TextChoices):
    OPEN = "OPEN", "Open"
    PARTIAL = "PARTIAL", "Partially repaid"
    SETTLED = "SETTLED", "Settled"
    WRITTEN_OFF = "WRITTEN_OFF", "Written off"
    CANCELLED = "CANCELLED", "Cancelled (sale voided)"


class DebtRecordQuerySet(models.QuerySet):
    def open_debts(self):
        return self.filter(status__in=[DebtStatus.OPEN, DebtStatus.PARTIAL])

    def overdue(self):
        return self.open_debts().filter(due_date__lt=timezone.localdate())

    def due_within(self, days: int):
        today = timezone.localdate()
        return self.open_debts().filter(
            due_date__gte=today, due_date__lte=today + dt.timedelta(days=days)
        )

    def settled(self):
        return self.filter(status=DebtStatus.SETTLED)

    def total_outstanding(self) -> Decimal:
        return money(self.open_debts().aggregate(t=Sum("balance"))["t"] or ZERO)


class DebtRecord(OwnedModel, TimeStampedModel):
    """
    One debt per credit sale. principal is the amount that was left UNPAID
    at the till - not the full sale value.

    Example: a 1,000 sale with 300 paid on the day creates a debt whose
    principal is 700.

    `owner` is copied from the originating sale, not inferred at read time.
    A debt has to stay attached to the manager who extended the credit even
    if the customer record is later reassigned - they are the person who has
    to collect it.
    """

    reference = models.CharField(max_length=40, unique=True, db_index=True, editable=False)
    customer = models.ForeignKey(
        "sales.Customer", on_delete=models.PROTECT, related_name="debts"
    )
    transaction = models.OneToOneField(
        "sales.Transaction", on_delete=models.PROTECT,
        related_name="debt_record", null=True, blank=True,
    )

    principal = models.DecimalField(
        max_digits=14, decimal_places=2,
        validators=[MinValueValidator(Decimal("0"))],
        help_text="Amount borrowed - the unpaid portion of the sale.",
    )
    amount_repaid = models.DecimalField(max_digits=14, decimal_places=2, default=ZERO)
    balance = models.DecimalField(
        max_digits=14, decimal_places=2, default=ZERO, db_index=True
    )

    status = models.CharField(
        max_length=12, choices=DebtStatus.choices, default=DebtStatus.OPEN, db_index=True
    )
    issued_date = models.DateField(default=timezone.localdate, db_index=True)
    due_date = models.DateField(db_index=True)
    settled_date = models.DateField(null=True, blank=True)

    written_off_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        related_name="debts_written_off", null=True, blank=True,
    )
    write_off_reason = models.TextField(blank=True)

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        related_name="debts_created", null=True, blank=True,
    )
    notes = models.TextField(blank=True)

    objects = DebtRecordQuerySet.as_manager()

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Debt record"
        indexes = [
            models.Index(fields=["customer", "status"]),
            models.Index(fields=["status", "due_date"]),
            models.Index(fields=["-created_at"]),
            models.Index(
                fields=["owner", "status", "due_date"], name="debt_owner_status_idx"
            ),
        ]
        constraints = [
            models.CheckConstraint(check=Q(principal__gte=0), name="debt_principal_non_negative"),
            models.CheckConstraint(check=Q(amount_repaid__gte=0), name="debt_repaid_non_negative"),
        ]

    def __str__(self):
        return f"{self.reference} - {self.customer.name} ({self.balance} outstanding)"

    def get_absolute_url(self):
        return reverse("credit:debt_detail", args=[self.pk])

    def save(self, *args, **kwargs):
        if not self.reference:
            from core.utils import generate_reference

            self.reference = generate_reference("DEBT", DebtRecord)
        if not self.due_date:
            from core.utils import default_due_date

            self.due_date = default_due_date()
        super().save(*args, **kwargs)

    # -- Recalculation from the ledger --------------------------------------
    def recalculate(self, commit: bool = True):
        """Rebuild amount_repaid / balance / status from the repayment rows."""
        if self.status == DebtStatus.CANCELLED:
            return self

        total = self.repayments.filter(is_reversed=False).aggregate(
            t=Sum("amount")
        )["t"] or ZERO

        self.amount_repaid = money(total)
        self.balance = money(max(self.principal - self.amount_repaid, ZERO))

        if self.status != DebtStatus.WRITTEN_OFF:
            if self.balance <= ZERO:
                self.status = DebtStatus.SETTLED
                if not self.settled_date:
                    self.settled_date = timezone.localdate()
            elif self.amount_repaid > ZERO:
                self.status = DebtStatus.PARTIAL
                self.settled_date = None
            else:
                self.status = DebtStatus.OPEN
                self.settled_date = None

        if commit:
            self.save(update_fields=[
                "amount_repaid", "balance", "status", "settled_date", "updated_at",
            ])
        return self

    # -- Derived -------------------------------------------------------------
    @property
    def is_settled(self) -> bool:
        return self.status == DebtStatus.SETTLED

    @property
    def is_open(self) -> bool:
        return self.status in {DebtStatus.OPEN, DebtStatus.PARTIAL}

    @property
    def is_overdue(self) -> bool:
        return self.is_open and self.due_date < timezone.localdate()

    @property
    def days_overdue(self) -> int:
        if not self.is_overdue:
            return 0
        return (timezone.localdate() - self.due_date).days

    @property
    def days_until_due(self) -> int:
        return (self.due_date - timezone.localdate()).days

    @property
    def repayment_percent(self) -> Decimal:
        if self.principal <= ZERO:
            return Decimal("100.00")
        return money(min(self.amount_repaid / self.principal * 100, Decimal("100")))

    @property
    def aging_bucket(self) -> str:
        """Standard accounts-receivable aging buckets."""
        if not self.is_open:
            return "SETTLED"
        d = self.days_overdue
        if d <= 0:
            return "CURRENT"
        if d <= 30:
            return "1-30"
        if d <= 60:
            return "31-60"
        if d <= 90:
            return "61-90"
        return "90+"

    @property
    def status_class(self) -> str:
        if self.status == DebtStatus.SETTLED:
            return "success"
        if self.status == DebtStatus.WRITTEN_OFF:
            return "dark"
        if self.status == DebtStatus.CANCELLED:
            return "secondary"
        if self.is_overdue:
            return "danger"
        if self.status == DebtStatus.PARTIAL:
            return "warning"
        return "info"

    @property
    def display_status(self) -> str:
        if self.is_overdue:
            return f"Overdue by {self.days_overdue} day{'s' if self.days_overdue != 1 else ''}"
        return self.get_status_display()

    @property
    def active_repayments(self):
        return self.repayments.filter(is_reversed=False).order_by("-paid_at")


# ---------------------------------------------------------------------------
# Repayments - the append-only ledger
# ---------------------------------------------------------------------------
class Repayment(TimeStampedModel):
    """
    One installment paid against one debt. NEVER edited, NEVER deleted.

    To correct an error an Admin marks the row is_reversed=True and a new
    reversal row is written. Both remain visible forever.
    """

    class Method(models.TextChoices):
        CASH = "CASH", "Cash"
        BANK = "BANK", "Bank transfer"
        MOBILE = "MOBILE", "Mobile money"
        CHEQUE = "CHEQUE", "Cheque"
        CARD = "CARD", "Card"
        GOODS_RETURN = "GOODS_RETURN", "Goods returned"
        WRITE_OFF = "WRITE_OFF", "Written off"

    reference = models.CharField(max_length=40, unique=True, db_index=True, editable=False)
    debt = models.ForeignKey(
        DebtRecord, on_delete=models.PROTECT, related_name="repayments"
    )
    amount = models.DecimalField(
        max_digits=14, decimal_places=2,
        validators=[MinValueValidator(Decimal("0.01"))],
    )
    method = models.CharField(max_length=14, choices=Method.choices, default=Method.CASH)
    paid_at = models.DateTimeField(default=timezone.now, db_index=True)

    # Snapshots so a single row tells the whole story without a join.
    balance_before = models.DecimalField(max_digits=14, decimal_places=2, default=ZERO)
    balance_after = models.DecimalField(max_digits=14, decimal_places=2, default=ZERO)

    external_reference = models.CharField(
        max_length=100, blank=True,
        help_text="Bank slip number, mobile-money transaction ID, cheque number.",
    )
    note = models.TextField(blank=True)
    received_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT,
        related_name="repayments_received", null=True, blank=True,
    )

    # -- Reversal (Admin correction path) -----------------------------------
    is_reversed = models.BooleanField(default=False, db_index=True)
    reversed_at = models.DateTimeField(null=True, blank=True)
    reversed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        related_name="repayments_reversed", null=True, blank=True,
    )
    reversal_reason = models.TextField(blank=True)

    class Meta:
        ordering = ["-paid_at", "-id"]
        indexes = [
            models.Index(fields=["debt", "-paid_at"]),
            models.Index(fields=["-paid_at"]),
            models.Index(fields=["is_reversed"]),
        ]
        constraints = [
            models.CheckConstraint(check=Q(amount__gt=0), name="repayment_amount_positive"),
        ]

    def __str__(self):
        return f"{self.reference} - {self.amount} on {self.debt.reference}"

    def get_absolute_url(self):
        return reverse("credit:debt_detail", args=[self.debt_id])

    def save(self, *args, **kwargs):
        if not self.reference:
            from core.utils import generate_reference

            self.reference = generate_reference("PAY", Repayment)
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise PermissionError(
            "Repayments are immutable. Reverse the entry instead of deleting it."
        )

    @property
    def is_full_settlement(self) -> bool:
        return self.balance_after <= ZERO

    @property
    def has_proof(self) -> bool:
        return self.proofs.exists()


class RepaymentProof(TimeStampedModel):
    """Receipt image / bank slip attached to a repayment."""

    repayment = models.ForeignKey(
        Repayment, on_delete=models.CASCADE, related_name="proofs"
    )
    file = models.FileField(
        upload_to=receipt_upload_path, validators=[validate_receipt_file]
    )
    caption = models.CharField(max_length=255, blank=True)
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        related_name="repayment_proofs", null=True, blank=True,
    )

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Repayment proof"

    def __str__(self):
        return f"Proof for {self.repayment.reference}"

    @property
    def reference_hint(self):
        return self.repayment.reference if self.repayment_id else "payment"

    @property
    def is_image(self) -> bool:
        return self.file.name.lower().rsplit(".", 1)[-1] in {"jpg", "jpeg", "png", "webp"}

    @property
    def filename(self) -> str:
        return self.file.name.rsplit("/", 1)[-1]
