"""
Customers, sales transactions, line items and receipt attachments.

Money rules enforced here:
  total_amount = subtotal - discount_amount + tax_amount
  balance_due  = total_amount - amount_paid
  payment_status is DERIVED from balance_due, never set by hand.

TransactionItem stores unit_cost as a SNAPSHOT taken at the moment of sale.
Changing a product's cost price tomorrow must not rewrite yesterday's margin.
"""
from decimal import Decimal

from django.conf import settings
from django.core.validators import MinValueValidator
from django.db import models
from django.db.models import F, Q, Sum
from django.urls import reverse
from django.utils import timezone

from core.models import AuthoredModel, OwnedModel, TimeStampedModel
from core.utils import ZERO, money, receipt_upload_path, validate_receipt_file


# ---------------------------------------------------------------------------
# Customers
# ---------------------------------------------------------------------------
class CustomerQuerySet(models.QuerySet):
    def active(self):
        return self.filter(is_active=True)

    def credit_approved(self):
        return self.active().filter(is_credit_approved=True)

    def with_debt(self):
        return self.filter(credit_account__outstanding_balance__gt=0)


class Customer(AuthoredModel, OwnedModel):
    class CustomerType(models.TextChoices):
        WALK_IN = "WALK_IN", "Walk-in"
        REGULAR = "REGULAR", "Regular"
        WHOLESALE = "WHOLESALE", "Wholesale"
        BUSINESS = "BUSINESS", "Business"

    name = models.CharField(max_length=160, db_index=True)
    # Unique PER OWNER. The same person can genuinely be a customer of two
    # different managers; a global constraint would hand the second manager a
    # "this phone is taken" error about a record they cannot see or open.
    phone = models.CharField(
        max_length=30,
        db_index=True,
        help_text="Primary identifier for a customer within your own list.",
    )
    alternate_phone = models.CharField(max_length=30, blank=True)
    email = models.EmailField(blank=True)
    address = models.TextField(blank=True)
    customer_type = models.CharField(
        max_length=12, choices=CustomerType.choices, default=CustomerType.REGULAR
    )

    is_credit_approved = models.BooleanField(
        default=False,
        help_text="Must be ticked before this customer can buy on 'Pay Later'.",
    )
    is_active = models.BooleanField(default=True, db_index=True)
    notes = models.TextField(blank=True)

    objects = CustomerQuerySet.as_manager()

    class Meta:
        ordering = ["name"]
        indexes = [
            models.Index(fields=["name"]),
            models.Index(fields=["phone"]),
            models.Index(fields=["is_active", "is_credit_approved"]),
            models.Index(fields=["owner", "is_active"], name="customer_owner_active_idx"),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["owner", "phone"], name="customer_phone_unique_per_owner"
            ),
        ]

    def __str__(self):
        return f"{self.name} - {self.phone}"

    def get_absolute_url(self):
        return reverse("sales:customer_detail", args=[self.pk])

    # -- Credit shortcuts (the CreditAccount is auto-created by a signal) ----
    @property
    def outstanding_balance(self) -> Decimal:
        account = getattr(self, "credit_account", None)
        return account.outstanding_balance if account else ZERO

    @property
    def credit_limit(self) -> Decimal:
        account = getattr(self, "credit_account", None)
        return account.credit_limit if account else ZERO

    @property
    def has_debt(self) -> bool:
        return self.outstanding_balance > 0

    @property
    def available_credit(self) -> Decimal:
        return money(max(self.credit_limit - self.outstanding_balance, ZERO))

    @property
    def total_purchases(self) -> Decimal:
        return money(
            self.transactions.active().aggregate(t=Sum("total_amount"))["t"] or ZERO
        )

    @property
    def transaction_count(self) -> int:
        return self.transactions.active().count()


# ---------------------------------------------------------------------------
# Transactions
# ---------------------------------------------------------------------------
class PaymentStatus(models.TextChoices):
    PAID = "PAID", "Paid"
    PARTIAL = "PARTIAL", "Partially paid"
    UNPAID = "UNPAID", "Unpaid (Credit)"
    REFUNDED = "REFUNDED", "Refunded"


class PaymentMethod(models.TextChoices):
    CASH = "CASH", "Cash"
    BANK = "BANK", "Bank transfer"
    MOBILE = "MOBILE", "Mobile money"
    CHEQUE = "CHEQUE", "Cheque"
    CARD = "CARD", "Card"
    CREDIT = "CREDIT", "Credit (Pay later)"
    MIXED = "MIXED", "Mixed"


class TransactionQuerySet(models.QuerySet):
    def active(self):
        """Everything except voided documents."""
        return self.filter(is_voided=False)

    def credit_sales(self):
        return self.active().filter(balance_due__gt=0)

    def paid(self):
        return self.active().filter(payment_status=PaymentStatus.PAID)

    def for_period(self, start, end):
        return self.active().filter(created_at__date__gte=start, created_at__date__lte=end)

    def today(self):
        return self.active().filter(created_at__date=timezone.localdate())

    def with_profit(self):
        """
        Annotate gross profit. Uses the cost snapshot on each line item.

        quantity is an integer and unit_cost a decimal, so the multiplication
        needs an explicit output_field or Django raises "mixed types".
        """
        dec = models.DecimalField(max_digits=16, decimal_places=2)
        cost = models.ExpressionWrapper(
            F("items__unit_cost") * F("items__quantity"), output_field=dec
        )
        return self.annotate(cost_total=Sum(cost)).annotate(
            profit=models.ExpressionWrapper(
                F("total_amount") - F("cost_total"), output_field=dec
            )
        )


class Transaction(OwnedModel, TimeStampedModel):
    # `reference` stays globally unique: it is quoted on printed receipts and
    # written into the stock ledger, so two managers must never issue the same
    # one. Only visibility is partitioned, not the numbering.
    reference = models.CharField(max_length=40, unique=True, db_index=True, editable=False)

    customer = models.ForeignKey(
        Customer,
        on_delete=models.PROTECT,
        related_name="transactions",
        null=True,
        blank=True,
        help_text="Leave blank for an anonymous cash sale. Required for credit.",
    )
    customer_name_snapshot = models.CharField(max_length=160, blank=True)
    customer_phone_snapshot = models.CharField(max_length=30, blank=True)

    # -- Money ---------------------------------------------------------------
    subtotal = models.DecimalField(max_digits=14, decimal_places=2, default=ZERO)
    discount_amount = models.DecimalField(
        max_digits=14, decimal_places=2, default=ZERO,
        validators=[MinValueValidator(Decimal("0"))],
    )
    tax_amount = models.DecimalField(max_digits=14, decimal_places=2, default=ZERO)
    total_amount = models.DecimalField(max_digits=14, decimal_places=2, default=ZERO, db_index=True)
    amount_paid = models.DecimalField(max_digits=14, decimal_places=2, default=ZERO)
    balance_due = models.DecimalField(max_digits=14, decimal_places=2, default=ZERO, db_index=True)

    payment_status = models.CharField(
        max_length=10, choices=PaymentStatus.choices,
        default=PaymentStatus.PAID, db_index=True, editable=False,
    )
    payment_method = models.CharField(
        max_length=10, choices=PaymentMethod.choices, default=PaymentMethod.CASH
    )
    due_date = models.DateField(
        null=True, blank=True, help_text="Only used when there is a balance to collect."
    )

    sold_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT,
        related_name="sales_made", null=True, blank=True,
    )
    notes = models.TextField(blank=True)

    # -- Void (Admin override) ----------------------------------------------
    is_voided = models.BooleanField(default=False, db_index=True)
    voided_at = models.DateTimeField(null=True, blank=True)
    voided_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        related_name="transactions_voided", null=True, blank=True,
    )
    void_reason = models.TextField(blank=True)

    objects = TransactionQuerySet.as_manager()

    class Meta:
        ordering = ["-created_at", "-id"]
        indexes = [
            models.Index(fields=["-created_at"]),
            models.Index(fields=["payment_status", "-created_at"]),
            models.Index(fields=["customer", "-created_at"]),
            models.Index(fields=["is_voided", "-created_at"]),
            models.Index(fields=["owner", "-created_at"], name="txn_owner_created_idx"),
        ]
        constraints = [
            models.CheckConstraint(
                check=Q(total_amount__gte=0), name="txn_total_non_negative"
            ),
            models.CheckConstraint(
                check=Q(amount_paid__gte=0), name="txn_paid_non_negative"
            ),
        ]

    def __str__(self):
        return self.reference

    def get_absolute_url(self):
        return reverse("sales:transaction_detail", args=[self.pk])

    def save(self, *args, **kwargs):
        if not self.reference:
            from core.utils import generate_reference

            self.reference = generate_reference("TXN", Transaction)
        if self.customer and not self.customer_name_snapshot:
            self.customer_name_snapshot = self.customer.name
            self.customer_phone_snapshot = self.customer.phone
        super().save(*args, **kwargs)

    # -- Derived -------------------------------------------------------------
    def recalculate_totals(self, commit: bool = True):
        """Recompute money fields from the line items. Single source of truth."""
        agg = self.items.aggregate(s=Sum("line_total"))
        self.subtotal = money(agg["s"] or ZERO)
        self.total_amount = money(self.subtotal - money(self.discount_amount) + money(self.tax_amount))
        self.balance_due = money(max(self.total_amount - money(self.amount_paid), ZERO))
        self.payment_status = self.derive_payment_status()
        if commit:
            self.save(update_fields=[
                "subtotal", "total_amount", "balance_due", "payment_status", "updated_at",
            ])
        return self

    def derive_payment_status(self) -> str:
        if self.is_voided:
            return PaymentStatus.REFUNDED
        if self.balance_due <= ZERO:
            return PaymentStatus.PAID
        if self.amount_paid > ZERO:
            return PaymentStatus.PARTIAL
        return PaymentStatus.UNPAID

    @property
    def is_credit_sale(self) -> bool:
        return self.balance_due > ZERO

    @property
    def item_count(self) -> int:
        return self.items.count()

    @property
    def total_quantity(self) -> int:
        return self.items.aggregate(q=Sum("quantity"))["q"] or 0

    @property
    def total_cost(self) -> Decimal:
        """Cost of goods sold, from the per-line snapshots. Admin-facing."""
        dec = models.DecimalField(max_digits=16, decimal_places=2)
        line_cost = models.ExpressionWrapper(
            F("unit_cost") * F("quantity"), output_field=dec
        )
        return money(self.items.aggregate(c=Sum(line_cost))["c"] or ZERO)

    @property
    def gross_profit(self) -> Decimal:
        return money(self.total_amount - self.total_cost)

    @property
    def profit_margin(self) -> Decimal:
        if self.total_amount <= ZERO:
            return ZERO
        return money(self.gross_profit / self.total_amount * 100)

    @property
    def status_class(self) -> str:
        return {
            PaymentStatus.PAID: "success",
            PaymentStatus.PARTIAL: "warning",
            PaymentStatus.UNPAID: "danger",
            PaymentStatus.REFUNDED: "secondary",
        }.get(self.payment_status, "secondary")

    @property
    def is_overdue(self) -> bool:
        return bool(
            self.due_date
            and self.balance_due > ZERO
            and not self.is_voided
            and self.due_date < timezone.localdate()
        )

    @property
    def customer_display(self) -> str:
        if self.customer:
            return f"{self.customer.name} ({self.customer.phone})"
        return self.customer_name_snapshot or "Walk-in customer"

    @property
    def debt(self):
        """The linked DebtRecord, if this sale created one."""
        return getattr(self, "debt_record", None)


class TransactionItem(models.Model):
    """
    One line on a sale. product_name / sku / unit_cost / unit_price are all
    SNAPSHOTS - the line must remain readable and auditable even if the
    product is later renamed, repriced or archived.
    """

    transaction = models.ForeignKey(
        Transaction, on_delete=models.CASCADE, related_name="items"
    )
    product = models.ForeignKey(
        "inventory.Product", on_delete=models.PROTECT, related_name="sale_items"
    )

    product_name = models.CharField(max_length=200)
    product_sku = models.CharField(max_length=60, blank=True)

    quantity = models.PositiveIntegerField(validators=[MinValueValidator(1)])
    unit_price = models.DecimalField(max_digits=12, decimal_places=2)
    unit_cost = models.DecimalField(
        max_digits=12, decimal_places=2, default=ZERO,
        help_text="Cost snapshot at time of sale. Protects historical margins.",
    )
    line_discount = models.DecimalField(max_digits=12, decimal_places=2, default=ZERO)
    line_total = models.DecimalField(max_digits=14, decimal_places=2, default=ZERO)

    class Meta:
        ordering = ["id"]
        verbose_name = "Sale line item"
        indexes = [models.Index(fields=["transaction"]), models.Index(fields=["product"])]
        constraints = [
            models.CheckConstraint(check=Q(quantity__gt=0), name="item_quantity_positive"),
        ]

    def __str__(self):
        return f"{self.quantity} x {self.product_name}"

    def save(self, *args, **kwargs):
        if self.product_id and not self.product_name:
            self.product_name = self.product.name
            self.product_sku = self.product.sku
        self.line_total = money(
            (money(self.unit_price) * self.quantity) - money(self.line_discount)
        )
        super().save(*args, **kwargs)

    @property
    def line_cost(self) -> Decimal:
        return money(self.unit_cost * self.quantity)

    @property
    def line_profit(self) -> Decimal:
        return money(self.line_total - self.line_cost)


# ---------------------------------------------------------------------------
# Receipts
# ---------------------------------------------------------------------------
class Receipt(TimeStampedModel):
    """Uploaded proof attached to a sale (photo of a paper receipt, bank slip)."""

    class Kind(models.TextChoices):
        SALE = "SALE", "Sale receipt"
        PAYMENT = "PAYMENT", "Payment proof"
        DELIVERY = "DELIVERY", "Delivery note"
        OTHER = "OTHER", "Other"

    transaction = models.ForeignKey(
        Transaction, on_delete=models.CASCADE, related_name="receipts"
    )
    file = models.FileField(
        upload_to=receipt_upload_path, validators=[validate_receipt_file]
    )
    kind = models.CharField(max_length=10, choices=Kind.choices, default=Kind.SALE)
    caption = models.CharField(max_length=255, blank=True)
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        related_name="receipts_uploaded", null=True, blank=True,
    )

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"Receipt for {self.transaction.reference}"

    @property
    def reference_hint(self):
        return self.transaction.reference if self.transaction_id else "misc"

    @property
    def is_image(self) -> bool:
        return self.file.name.lower().rsplit(".", 1)[-1] in {"jpg", "jpeg", "png", "webp"}

    @property
    def is_pdf(self) -> bool:
        return self.file.name.lower().endswith(".pdf")

    @property
    def filename(self) -> str:
        return self.file.name.rsplit("/", 1)[-1]
