"""
Products, categories, suppliers and the immutable stock movement ledger.

Design rule: Product.stock_quantity is a *cached* value. The authoritative
history is StockMovement. Every change to stock_quantity must go through
inventory.services.apply_stock_movement() so the two can never drift.
"""
from decimal import Decimal

from django.core.validators import MinValueValidator
from django.db import models
from django.db.models import F, Sum
from django.urls import reverse

from core.models import AuthoredModel, OwnedModel, SoftDeleteModel, TimeStampedModel


class Category(TimeStampedModel):
    name = models.CharField(max_length=120, unique=True)
    slug = models.SlugField(max_length=140, unique=True, blank=True)
    description = models.TextField(blank=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["name"]
        verbose_name_plural = "Categories"

    def __str__(self):
        return self.name

    def save(self, *args, **kwargs):
        if not self.slug:
            from django.utils.text import slugify

            self.slug = slugify(self.name)[:140]
        super().save(*args, **kwargs)

    def get_absolute_url(self):
        return reverse("inventory:category_list")

    @property
    def product_count(self):
        return self.products.alive().count()


class Supplier(TimeStampedModel):
    name = models.CharField(max_length=160, unique=True)
    contact_person = models.CharField(max_length=120, blank=True)
    phone = models.CharField(max_length=30, blank=True)
    email = models.EmailField(blank=True)
    address = models.TextField(blank=True)
    is_active = models.BooleanField(default=True)
    notes = models.TextField(blank=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name

    def get_absolute_url(self):
        return reverse("inventory:supplier_list")


class ProductQuerySet(models.QuerySet):
    def alive(self):
        return self.filter(is_deleted=False)

    def active(self):
        return self.alive().filter(is_active=True)

    def low_stock(self):
        """At or below the alert threshold, but not yet zero-or-below."""
        return self.active().filter(
            stock_quantity__lte=F("low_stock_threshold"), stock_quantity__gt=0
        )

    def out_of_stock(self):
        return self.active().filter(stock_quantity__lte=0)

    def needs_attention(self):
        return self.active().filter(stock_quantity__lte=F("low_stock_threshold"))

    def with_stock_value(self):
        # stock_quantity is an integer and the prices are decimals, so the
        # database needs an explicit output type for the product. Without
        # output_field Django raises "Expression contains mixed types".
        dec = models.DecimalField(max_digits=16, decimal_places=2)
        return self.annotate(
            stock_value=models.ExpressionWrapper(
                F("stock_quantity") * F("cost_price"), output_field=dec
            ),
            retail_value=models.ExpressionWrapper(
                F("stock_quantity") * F("selling_price"), output_field=dec
            ),
        )


class Product(AuthoredModel, OwnedModel, SoftDeleteModel):
    class Unit(models.TextChoices):
        PIECE = "PIECE", "Piece"
        BOX = "BOX", "Box"
        CARTON = "CARTON", "Carton"
        KG = "KG", "Kilogram"
        LITRE = "LITRE", "Litre"
        METER = "METER", "Meter"
        PACK = "PACK", "Pack"

    # SKU and barcode are unique PER OWNER, not globally. Two managers keep
    # separate stock lists and must both be able to use SKU "SOF-00001" for
    # their own sofa without one of them being told it is taken by a product
    # they are not even allowed to see.
    sku = models.CharField(
        max_length=60,
        db_index=True,
        help_text="Internal stock keeping unit. Auto-generated if left blank.",
    )
    barcode = models.CharField(
        max_length=60,
        blank=True,
        null=True,
        db_index=True,
        help_text="EAN/UPC scanned at the counter. Leave blank if none.",
    )
    name = models.CharField(max_length=200, db_index=True)
    description = models.TextField(blank=True)
    category = models.ForeignKey(
        Category,
        on_delete=models.PROTECT,
        related_name="products",
        null=True,
        blank=True,
    )
    supplier = models.ForeignKey(
        Supplier,
        on_delete=models.SET_NULL,
        related_name="products",
        null=True,
        blank=True,
    )
    unit = models.CharField(max_length=10, choices=Unit.choices, default=Unit.PIECE)

    # -- Money --------------------------------------------------------------
    cost_price = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=Decimal("0.00"),
        validators=[MinValueValidator(Decimal("0"))],
        help_text="What you pay. Visible to Administrators only.",
    )
    selling_price = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=Decimal("0.00"),
        validators=[MinValueValidator(Decimal("0"))],
        help_text="What the customer pays.",
    )

    # -- Stock --------------------------------------------------------------
    stock_quantity = models.IntegerField(
        default=0,
        help_text="Cached running total. Derived from StockMovement history.",
    )
    low_stock_threshold = models.PositiveIntegerField(
        default=5, help_text="Raise a low-stock alert at or below this quantity."
    )
    allow_negative_stock = models.BooleanField(
        default=False,
        help_text="Permit selling below zero (back-orders). Off by default.",
    )

    image = models.ImageField(upload_to="products/%Y/%m/", blank=True, null=True)
    is_active = models.BooleanField(default=True, db_index=True)

    objects = ProductQuerySet.as_manager()

    class Meta:
        ordering = ["name"]
        indexes = [
            models.Index(fields=["is_active", "is_deleted"]),
            models.Index(fields=["category", "is_active"]),
            models.Index(fields=["name"]),
            models.Index(
                fields=["owner", "is_active", "is_deleted"],
                name="product_owner_active_idx",
            ),
        ]
        constraints = [
            models.CheckConstraint(
                check=models.Q(cost_price__gte=0), name="product_cost_price_non_negative"
            ),
            models.CheckConstraint(
                check=models.Q(selling_price__gte=0),
                name="product_selling_price_non_negative",
            ),
            models.UniqueConstraint(
                fields=["owner", "sku"], name="product_sku_unique_per_owner"
            ),
            models.UniqueConstraint(
                fields=["owner", "barcode"],
                condition=models.Q(barcode__isnull=False),
                name="product_barcode_unique_per_owner",
            ),
        ]

    def __str__(self):
        return f"{self.name} ({self.sku})"

    def get_absolute_url(self):
        return reverse("inventory:product_detail", args=[self.pk])

    def save(self, *args, **kwargs):
        if not self.sku:
            self.sku = self._generate_sku()
        if not self.barcode:
            self.barcode = None  # keep UNIQUE happy across many blank rows
        super().save(*args, **kwargs)

    def _generate_sku(self):
        """
        Next free SKU *within this owner's catalogue*.

        Scoped to the owner because the uniqueness constraint is scoped to the
        owner. Scanning globally would make two managers' sequences interleave
        and leak the size of each other's catalogue through the numbering.
        """
        prefix = "".join(w[0] for w in self.name.split()[:3]).upper() or "PRD"
        last = (
            Product.objects.filter(owner_id=self.owner_id, sku__startswith=f"{prefix}-")
            .order_by("-sku")
            .values_list("sku", flat=True)
            .first()
        )
        seq = int(last.split("-")[-1]) + 1 if last and last.split("-")[-1].isdigit() else 1
        return f"{prefix}-{seq:05d}"

    # -- Derived financials (Admin-facing) ----------------------------------
    @property
    def profit_per_unit(self) -> Decimal:
        return self.selling_price - self.cost_price

    @property
    def margin_percent(self) -> Decimal:
        if not self.selling_price:
            return Decimal("0.00")
        return ((self.selling_price - self.cost_price) / self.selling_price * 100).quantize(
            Decimal("0.01")
        )

    @property
    def markup_percent(self) -> Decimal:
        if not self.cost_price:
            return Decimal("0.00")
        return ((self.selling_price - self.cost_price) / self.cost_price * 100).quantize(
            Decimal("0.01")
        )

    @property
    def stock_value(self) -> Decimal:
        return self.cost_price * self.stock_quantity

    @property
    def retail_value(self) -> Decimal:
        return self.selling_price * self.stock_quantity

    # -- Stock state --------------------------------------------------------
    @property
    def is_low_stock(self) -> bool:
        return 0 < self.stock_quantity <= self.low_stock_threshold

    @property
    def is_out_of_stock(self) -> bool:
        return self.stock_quantity <= 0

    @property
    def stock_status(self) -> str:
        if self.is_out_of_stock:
            return "OUT"
        if self.is_low_stock:
            return "LOW"
        return "OK"

    @property
    def stock_status_label(self) -> str:
        return {"OUT": "Out of stock", "LOW": "Low stock", "OK": "In stock"}[
            self.stock_status
        ]

    @property
    def stock_status_class(self) -> str:
        return {"OUT": "danger", "LOW": "warning", "OK": "success"}[self.stock_status]

    def recalculate_stock_from_ledger(self) -> int:
        """
        Rebuild stock_quantity from the movement ledger. This is the
        reconciliation escape hatch if anything ever looks wrong.
        """
        total = self.stock_movements.aggregate(total=Sum("quantity_delta"))["total"] or 0
        Product.objects.filter(pk=self.pk).update(stock_quantity=total)
        self.refresh_from_db(fields=["stock_quantity"])
        return total


class MovementType(models.TextChoices):
    RESTOCK = "RESTOCK", "Restock / Purchase in"
    SALE = "SALE", "Sale"
    RETURN_IN = "RETURN_IN", "Customer return (in)"
    RETURN_OUT = "RETURN_OUT", "Return to supplier (out)"
    ADJUSTMENT = "ADJUSTMENT", "Manual adjustment"
    DAMAGE = "DAMAGE", "Damage / write-off"
    VOID_REVERSAL = "VOID_REVERSAL", "Reversal of voided sale"
    OPENING = "OPENING", "Opening balance"


class StockMovementQuerySet(models.QuerySet):
    def inbound(self):
        return self.filter(quantity_delta__gt=0)

    def outbound(self):
        return self.filter(quantity_delta__lt=0)


class StockMovement(TimeStampedModel):
    """
    Append-only ledger. One row per stock event, forever.

    quantity_delta is signed: +12 for a restock, -3 for a sale.
    quantity_before / quantity_after are snapshots, so any row can be
    audited in isolation without replaying the whole history.
    """

    product = models.ForeignKey(
        Product, on_delete=models.PROTECT, related_name="stock_movements"
    )
    movement_type = models.CharField(
        max_length=15, choices=MovementType.choices, db_index=True
    )
    quantity_delta = models.IntegerField(
        help_text="Signed change. Positive = stock in, negative = stock out."
    )
    quantity_before = models.IntegerField()
    quantity_after = models.IntegerField()

    unit_cost = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        null=True,
        blank=True,
        help_text="Cost at the time of the movement (restocks).",
    )
    unit_price = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        null=True,
        blank=True,
        help_text="Selling price at the time of the movement (sales).",
    )

    reference = models.CharField(
        max_length=60,
        blank=True,
        db_index=True,
        help_text="Linked document, e.g. TXN-20260821-0007 or a supplier invoice no.",
    )
    reason = models.CharField(max_length=255, blank=True)
    performed_by = models.ForeignKey(
        "accounts.User",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="stock_movements",
    )

    objects = StockMovementQuerySet.as_manager()

    class Meta:
        ordering = ["-created_at", "-id"]
        verbose_name = "Stock movement"
        indexes = [
            models.Index(fields=["product", "-created_at"]),
            models.Index(fields=["movement_type", "-created_at"]),
            models.Index(fields=["reference"]),
        ]

    def __str__(self):
        sign = "+" if self.quantity_delta >= 0 else ""
        return f"{self.product.sku} {sign}{self.quantity_delta} ({self.get_movement_type_display()})"

    @property
    def is_inbound(self) -> bool:
        return self.quantity_delta > 0

    @property
    def abs_quantity(self) -> int:
        return abs(self.quantity_delta)

    def delete(self, *args, **kwargs):
        raise PermissionError(
            "Stock movements are immutable. Post a correcting ADJUSTMENT instead."
        )
