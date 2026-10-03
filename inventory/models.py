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

from core.models import (
    AuthoredModel,
    OwnedModel,
    SoftDeleteModel,
    TimeStampedModel,
    note_tag_field,
)


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
    note_tag = note_tag_field()

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name

    def get_absolute_url(self):
        return reverse("inventory:supplier_list")


def cost_used():
    """
    What one unit is held to cost, as a database expression: the owner's own
    figure when he has set one in the Audit, otherwise the cost price.

    The Python twin is Product.audit_unit_cost. Every valuation of the stock
    goes through one or the other, so the product page, the products list,
    the inventory report and the Audit all agree on what the shelf is worth.
    """
    from django.db.models.functions import Coalesce

    return Coalesce(F("audit_cost"), F("cost_price"))


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
        #
        # At the owner's own cost where he has set one (Product.audit_cost),
        # like everything else that values the stock - see cost_used().
        dec = models.DecimalField(max_digits=16, decimal_places=2)
        return self.annotate(
            stock_value=models.ExpressionWrapper(
                F("stock_quantity") * cost_used(), output_field=dec
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
    # `choices` is kept for the built-in wording and for a readable admin, but
    # the column is NOT limited to it: a yard that sells by the jerrycan adds
    # one from inside the dropdown and the code is stored here like any other.
    # 32, not 10, because a unit somebody types is not a two-letter code.
    # NO `choices=` ON PURPOSE.
    #
    # `Unit` above is still the shipped list - it seeds the editable one and
    # supplies the fallback wording - but binding it to the field here would
    # make Model.full_clean() refuse every unit added since deploy, which is
    # exactly the thing this field was opened up for: a product form posting
    # PALLET was told "Select a valid choice" even though PALLET was in the
    # dropdown it came from. What may be stored is decided by the list, in
    # one place - see api.serializers._validate_unit and core.forms.unit_choices.
    unit = models.CharField(max_length=32, default=Unit.PIECE)

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

    # -- What one unit really costs, in the owner's judgement ---------------
    # cost_price is what the batches can measure: materials, plus whatever
    # was paid against the batch. It cannot see the electricity, the wages of
    # the people who stacked the blocks, the truck that brought the sand -
    # costs that change day by day and belong to no single batch. Only the
    # owner, looking at everything, can say what one unit truly costs; the
    # Audit asks them, and keeps the answer here. See reports/audit.py.
    #
    # Deliberately NOT copied into cost_price. The profit report takes the
    # running costs off separately; folding them into the unit cost as well
    # would count the same electricity twice.
    audit_cost = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        null=True,
        blank=True,
        validators=[MinValueValidator(Decimal("0"))],
        help_text="What one unit really costs, all costs included - the owner's figure.",
    )
    audit_cost_note = models.CharField(max_length=255, blank=True)
    audit_cost_set_at = models.DateTimeField(null=True, blank=True)
    audit_cost_set_by = models.ForeignKey(
        "accounts.User",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )

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

    def get_unit_display(self) -> str:
        """
        The wording for this product's unit, from the editable list.

        DELIBERATELY SHADOWING DJANGO'S GENERATED METHOD
        -----------------------------------------------
        `choices` on a field makes Django attach a `get_unit_display` that can
        only ever return one of the seven it shipped with - so a unit added
        from the dropdown would render as "BUCKET" everywhere. Django only
        attaches its version when the class does not already define one
        (Field.contribute_to_class checks hasattr), so defining it here wins.

        Doing it this way rather than renaming the method means every caller
        keeps working untouched: the serializers that read
        `source="get_unit_display"`, the templates, and the phone.
        """
        from core.models import coded_label

        return coded_label("PRODUCT_UNIT", self.unit, self.Unit.choices)

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
        Next free SKU across the whole catalogue.

        Scanned globally, not per owner. There is one catalogue now (see
        core.scoping), so a per-owner sequence would put two different products
        on the same shelf under the same SKU - and a SKU that identifies two
        things identifies neither.

        The database constraint is still (owner, sku); this generator simply
        never hands out one that is already taken.
        """
        prefix = "".join(w[0] for w in self.name.split()[:3]).upper() or "PRD"
        last = (
            Product.objects.filter(sku__startswith=f"{prefix}-")
            .order_by("-sku")
            .values_list("sku", flat=True)
            .first()
        )
        seq = int(last.split("-")[-1]) + 1 if last and last.split("-")[-1].isdigit() else 1
        return f"{prefix}-{seq:05d}"

    # -- Derived financials (Admin-facing) ----------------------------------
    # All at the cost the owner holds a unit to - his own figure from the
    # Audit when he has set one, else the cost price (audit_unit_cost). He
    # sets that figure precisely because the batches cannot see the
    # electricity and the wages; a product page that went on showing the
    # batch figure would contradict the Audit he has just corrected.
    #
    # What a SALE records as its cost stays the cost price (see
    # TransactionItem.unit_cost): the profit report takes the running costs
    # off separately, and his figure already has them in it.
    @property
    def profit_per_unit(self) -> Decimal:
        return self.selling_price - self.audit_unit_cost

    @property
    def margin_percent(self) -> Decimal:
        if not self.selling_price:
            return Decimal("0.00")
        return ((self.selling_price - self.audit_unit_cost) / self.selling_price * 100).quantize(
            Decimal("0.01")
        )

    @property
    def markup_percent(self) -> Decimal:
        cost = self.audit_unit_cost
        if not cost:
            return Decimal("0.00")
        return ((self.selling_price - cost) / cost * 100).quantize(Decimal("0.01"))

    @property
    def stock_value(self) -> Decimal:
        return self.audit_unit_cost * self.stock_quantity

    @property
    def has_your_cost(self) -> bool:
        return self.audit_cost is not None

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

    @property
    def audit_unit_cost(self) -> Decimal:
        """The owner's figure when there is one, otherwise what batches measured."""
        return self.audit_cost if self.audit_cost is not None else self.cost_price


class ProductCostEstimate(models.Model):
    """
    Every time the owner said what a product really costs.

    Append-only. The current figure lives on the product (Product.audit_cost);
    this is how it got there - so "why did the margin on hollow blocks drop in
    March?" can be answered with "because in March the owner decided they
    cost 3 Birr more than he had thought", and by whom.
    """

    product = models.ForeignKey(
        Product, on_delete=models.CASCADE, related_name="cost_estimates"
    )
    #: None when the owner cleared their figure.
    cost = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    previous = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    #: What the batches said at that moment, for comparison later.
    system_cost = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    note = models.CharField(max_length=255, blank=True)
    set_by = models.ForeignKey(
        "accounts.User",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    set_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-set_at", "-id"]
        verbose_name = "Product cost estimate"

    def __str__(self):
        return f"{self.product} -> {self.cost}"

    def delete(self, *args, **kwargs):
        raise PermissionError("Cost estimates are a history and cannot be deleted.")


class MovementType(models.TextChoices):
    RESTOCK = "RESTOCK", "Restock / Purchase in"
    SALE = "SALE", "Sale"
    RETURN_IN = "RETURN_IN", "Customer return (in)"
    RETURN_OUT = "RETURN_OUT", "Return to supplier (out)"
    ADJUSTMENT = "ADJUSTMENT", "Manual adjustment"
    DAMAGE = "DAMAGE", "Damage / write-off"
    VOID_REVERSAL = "VOID_REVERSAL", "Reversal of voided sale"
    OPENING = "OPENING", "Opening balance"
    # Stock that was made rather than bought. Kept distinct from RESTOCK so the
    # inventory report can answer "how much did we make" separately from "how
    # much did we buy" - in a yard that manufactures, those are different
    # questions with different people accountable for them.
    PRODUCTION = "PRODUCTION", "Produced in-house"
    PRODUCTION_REVERSAL = "PRODUCTION_REVERSAL", "Reversal of a production run"


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
        # 20, not 15: PRODUCTION_REVERSAL is nineteen characters. Widening a
        # choices column is a free migration; discovering the truncation in
        # production is not.
        max_length=20,
        choices=MovementType.choices,
        db_index=True,
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

    # -- A restock that was entered wrong -----------------------------------
    # As on production.MaterialMovement: what was typed stays, the row says
    # what it should have been, and the money is counted from that. The
    # stock changes only when asked - see reports/corrections.py.
    corrected_quantity = models.IntegerField(
        null=True, blank=True,
        help_text="What was really received; 0 when the restock never happened.",
    )
    corrected_unit_cost = models.DecimalField(
        max_digits=12, decimal_places=2, null=True, blank=True
    )
    corrected_at = models.DateTimeField(null=True, blank=True)
    corrected_by = models.ForeignKey(
        "accounts.User",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    correction_note = models.CharField(max_length=255, blank=True)

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

    @property
    def is_corrected(self) -> bool:
        return self.corrected_at is not None

    @property
    def counted_quantity(self) -> int:
        """What the money is counted from: the correction, else what was typed."""
        if self.corrected_quantity is not None:
            return self.corrected_quantity
        return self.quantity_delta

    @property
    def counted_unit_cost(self):
        if self.corrected_unit_cost is not None:
            return self.corrected_unit_cost
        return self.unit_cost

    def delete(self, *args, **kwargs):
        raise PermissionError(
            "Stock movements are immutable. Post a correcting ADJUSTMENT instead."
        )
