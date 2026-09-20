"""
Raw materials, recipes, and the production runs that turn one into the other.

WHY THIS IS A SEPARATE APP FROM `inventory`
-------------------------------------------
A block yard holds two kinds of stock and they behave differently:

  inventory.Product   finished goods. Whole units. Sold at the till.
  production.RawMaterial
                      cement, sand, water. Fractional quantities. Never sold.

Modelling both as Product would mean an integer `stock_quantity` for something
measured in cubic metres, and a raw material one careless queryset away from
appearing in the till's product picker. The two are related by production, not
by being the same thing, so they get their own tables and their own ledger.

THE INVARIANT
-------------
Same rule as inventory, restated for materials:

    SUM(MaterialMovement.quantity_delta) == RawMaterial.quantity_in_stock

Nothing outside `production.services` may write to `quantity_in_stock`.

WHY DECIMAL AND NOT FLOAT
-------------------------
0.1 + 0.2 != 0.3 in binary floating point, and a stock figure that drifts by
a millionth per movement is a stock figure nobody can reconcile against a
delivery note. Every quantity here is Decimal with three places, which is
finer than any yard actually weighs to.
"""
from decimal import Decimal

from django.core.validators import MinValueValidator
from django.db import models
from django.urls import reverse

from core.models import (
    AuthoredModel,
    OwnedModel,
    SoftDeleteModel,
    TimeStampedModel,
    note_tag_field,
)

#: Quantities are stored to three decimal places - 0.001 kg is a gram, and no
#: yard weighs finer than that. Shared so every column and every serializer
#: agrees without anyone having to remember the number.
QUANTITY_DECIMALS = 3
QUANTITY_DIGITS = 14
ZERO = Decimal("0.000")


class MaterialUnit(models.TextChoices):
    """
    How a material is measured.

    Deliberately not the same list as Product.Unit: nobody sells a cubic metre
    of hollow blocks, and nobody buys cement by the "piece". Sharing one list
    would put wrong options in both dropdowns.
    """

    KG = "KG", "Kilogram"
    TONNE = "TONNE", "Tonne"
    BAG = "BAG", "Bag"
    CUBIC_METER = "M3", "Cubic metre"
    LITRE = "LITRE", "Litre"
    PIECE = "PIECE", "Piece"
    METER = "METER", "Metre"
    ROLL = "ROLL", "Roll"


class MaterialMovementType(models.TextChoices):
    PURCHASE = "PURCHASE", "Delivery received"
    CONSUMED = "CONSUMED", "Used in production"
    WASTE = "WASTE", "Spoiled / written off"
    RETURN_OUT = "RETURN_OUT", "Returned to supplier"
    ADJUSTMENT = "ADJUSTMENT", "Stock count correction"
    PRODUCTION_REVERSAL = "PRODUCTION_REVERSAL", "Returned by a reversed run"
    OPENING = "OPENING", "Opening balance"


class RawMaterialQuerySet(models.QuerySet):
    def alive(self):
        return self.filter(is_deleted=False)

    def active(self):
        return self.alive().filter(is_active=True)

    def low(self):
        """At or below the reorder level, but not yet empty."""
        return self.active().filter(
            quantity_in_stock__lte=models.F("reorder_level"),
            quantity_in_stock__gt=0,
        )

    def empty(self):
        return self.active().filter(quantity_in_stock__lte=0)

    def needs_ordering(self):
        return self.active().filter(quantity_in_stock__lte=models.F("reorder_level"))

    def with_value(self):
        dec = models.DecimalField(max_digits=18, decimal_places=2)
        return self.annotate(
            stock_value=models.ExpressionWrapper(
                models.F("quantity_in_stock") * models.F("unit_cost"), output_field=dec
            )
        )


class RawMaterial(AuthoredModel, OwnedModel, SoftDeleteModel):
    """One thing you buy in and consume: a cement, a sand, a pigment."""

    # Unique per owner for the same reason a SKU is: two managers keep separate
    # yards and must both be able to call their cement "CEM".
    code = models.CharField(
        max_length=40,
        db_index=True,
        help_text="Short code for the store card. Auto-generated if left blank.",
    )
    name = models.CharField(max_length=160, db_index=True)
    description = models.TextField(blank=True)
    # `choices` keeps the built-in wording and a readable admin, but the
    # column is NOT limited to it - a store that measures in jerrycans adds
    # one from inside the dropdown. See get_unit_display below. 32, not 10,
    # because a unit somebody types is not a two-letter code.
    # No `choices=` - see the twin of this on inventory.Product: the list is
    # editable, so binding the shipped eight to the field would have the model
    # refuse a unit the dropdown had just offered.
    unit = models.CharField(max_length=32, default=MaterialUnit.KG)
    supplier = models.ForeignKey(
        "inventory.Supplier",
        on_delete=models.SET_NULL,
        related_name="raw_materials",
        null=True,
        blank=True,
    )

    quantity_in_stock = models.DecimalField(
        max_digits=QUANTITY_DIGITS,
        decimal_places=QUANTITY_DECIMALS,
        default=ZERO,
        help_text="Cached running total. Derived from MaterialMovement history.",
    )
    reorder_level = models.DecimalField(
        max_digits=QUANTITY_DIGITS,
        decimal_places=QUANTITY_DECIMALS,
        default=ZERO,
        validators=[MinValueValidator(Decimal("0"))],
        help_text="Warn at or below this quantity.",
    )
    unit_cost = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=Decimal("0.00"),
        validators=[MinValueValidator(Decimal("0"))],
        help_text="What one unit costs to buy. Drives the cost of a batch.",
    )
    allow_negative_stock = models.BooleanField(
        default=False,
        help_text="Permit consuming below zero. Off by default.",
    )
    is_active = models.BooleanField(default=True, db_index=True)

    objects = RawMaterialQuerySet.as_manager()

    class Meta:
        ordering = ["name"]
        verbose_name = "Raw material"
        indexes = [
            models.Index(fields=["is_active", "is_deleted"]),
            models.Index(fields=["owner", "is_active", "is_deleted"],
                         name="material_owner_active_idx"),
            models.Index(fields=["name"]),
        ]
        constraints = [
            models.CheckConstraint(
                check=models.Q(unit_cost__gte=0),
                name="material_unit_cost_non_negative",
            ),
            models.UniqueConstraint(
                fields=["owner", "code"], name="material_code_unique_per_owner"
            ),
        ]

    def __str__(self):
        return f"{self.name} ({self.code})"

    def get_unit_display(self) -> str:
        """
        The wording for this material's unit, from the editable list.

        Shadows Django's generated method on purpose - see the twin of this on
        inventory.Product for why, and why every existing caller keeps working
        without being touched.
        """
        from core.models import coded_label

        return coded_label("MATERIAL_UNIT", self.unit, MaterialUnit.choices)

    def get_absolute_url(self):
        return reverse("production:material_detail", args=[self.pk])

    def save(self, *args, **kwargs):
        if not self.code:
            self.code = self._generate_code()
        super().save(*args, **kwargs)

    def _generate_code(self) -> str:
        """
        Next free code across the whole store, e.g. CEM-001.

        Scanned globally, not per owner. There is one yard now (see
        core.scoping), so two people would otherwise both be handed "RO-001"
        for two different tins of red oxide and both codes would sit in the
        same list, on the same shelf, meaning different things.

        The database constraint is still (owner, code); this generator simply
        never hands out a code that is already in use by anyone, which is what
        keeps the shared list readable.
        """
        prefix = "".join(word[0] for word in self.name.split()[:3]).upper() or "MAT"
        prefix = prefix[:6]
        last = (
            RawMaterial.objects.filter(code__startswith=f"{prefix}-")
            .order_by("-code")
            .values_list("code", flat=True)
            .first()
        )
        tail = last.split("-")[-1] if last else ""
        seq = int(tail) + 1 if tail.isdigit() else 1
        return f"{prefix}-{seq:03d}"

    # -- Derived ------------------------------------------------------------
    @property
    def stock_value(self) -> Decimal:
        return (self.quantity_in_stock * self.unit_cost).quantize(Decimal("0.01"))

    @property
    def is_empty(self) -> bool:
        return self.quantity_in_stock <= 0

    @property
    def is_low(self) -> bool:
        return not self.is_empty and self.quantity_in_stock <= self.reorder_level

    @property
    def stock_status(self) -> str:
        if self.is_empty:
            return "OUT"
        return "LOW" if self.is_low else "OK"

    @property
    def stock_status_label(self) -> str:
        return {"OUT": "Out of stock", "LOW": "Low stock", "OK": "In stock"}[
            self.stock_status
        ]

    def recalculate_from_ledger(self, commit: bool = True) -> Decimal:
        total = self.movements.aggregate(total=models.Sum("quantity_delta"))["total"]
        total = total if total is not None else ZERO
        if commit and total != self.quantity_in_stock:
            RawMaterial.objects.filter(pk=self.pk).update(quantity_in_stock=total)
            self.quantity_in_stock = total
        return total


class MaterialMovement(TimeStampedModel):
    """
    Append-only ledger for materials. One row per event, forever.

    The mirror image of inventory.StockMovement, and immutable for the same
    reason: a store card you can edit is a store card that proves nothing.
    """

    material = models.ForeignKey(
        RawMaterial, on_delete=models.PROTECT, related_name="movements"
    )
    movement_type = models.CharField(
        max_length=20, choices=MaterialMovementType.choices, db_index=True
    )
    quantity_delta = models.DecimalField(
        max_digits=QUANTITY_DIGITS,
        decimal_places=QUANTITY_DECIMALS,
        help_text="Signed change. Positive = received, negative = used.",
    )
    quantity_before = models.DecimalField(
        max_digits=QUANTITY_DIGITS, decimal_places=QUANTITY_DECIMALS
    )
    quantity_after = models.DecimalField(
        max_digits=QUANTITY_DIGITS, decimal_places=QUANTITY_DECIMALS
    )
    unit_cost = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        null=True,
        blank=True,
        help_text="Cost per unit at the time of the movement.",
    )
    reference = models.CharField(
        max_length=60,
        blank=True,
        db_index=True,
        help_text="Linked document: a production run, or a supplier invoice.",
    )
    reason = models.CharField(max_length=255, blank=True)
    performed_by = models.ForeignKey(
        "accounts.User",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="material_movements",
    )

    class Meta:
        ordering = ["-created_at", "-id"]
        verbose_name = "Material movement"
        indexes = [
            models.Index(fields=["material", "-created_at"]),
            models.Index(fields=["movement_type", "-created_at"]),
            models.Index(fields=["reference"]),
        ]

    def __str__(self):
        sign = "+" if self.quantity_delta >= 0 else ""
        return (
            f"{self.material.code} {sign}{self.quantity_delta} "
            f"({self.get_movement_type_display()})"
        )

    @property
    def is_inbound(self) -> bool:
        return self.quantity_delta > 0

    @property
    def abs_quantity(self) -> Decimal:
        return abs(self.quantity_delta)

    @property
    def line_cost(self) -> Decimal:
        if self.unit_cost is None:
            return Decimal("0.00")
        return (self.abs_quantity * self.unit_cost).quantize(Decimal("0.01"))

    def delete(self, *args, **kwargs):
        raise PermissionError(
            "Material movements are immutable. Post a correcting ADJUSTMENT "
            "instead."
        )


class Recipe(AuthoredModel):
    """
    What one product is made of.

    `output_quantity` exists because a yard thinks in mixes, not in units: one
    mixer load makes sixty blocks and takes two bags of cement. Forcing that
    into a per-block figure gives 0.033 bags, which is a number nobody can
    check against reality. Set it to 1 to write a per-unit recipe instead.
    """

    product = models.OneToOneField(
        "inventory.Product", on_delete=models.CASCADE, related_name="recipe"
    )
    output_quantity = models.PositiveIntegerField(
        default=1,
        help_text="How many finished units the quantities below produce.",
    )
    notes = models.TextField(blank=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        verbose_name = "Recipe"
        ordering = ["product__name"]

    def __str__(self):
        return f"Recipe for {self.product.name}"

    @property
    def material_cost(self) -> Decimal:
        """What one batch of `output_quantity` units costs in materials."""
        total = sum((item.line_cost for item in self.items.all()), Decimal("0.00"))
        return total.quantize(Decimal("0.01"))

    @property
    def cost_per_unit(self) -> Decimal:
        if not self.output_quantity:
            return Decimal("0.00")
        return (self.material_cost / self.output_quantity).quantize(Decimal("0.01"))


class RecipeItem(models.Model):
    """One material line of a recipe."""

    recipe = models.ForeignKey(Recipe, on_delete=models.CASCADE, related_name="items")
    material = models.ForeignKey(
        RawMaterial, on_delete=models.PROTECT, related_name="recipe_items"
    )
    quantity = models.DecimalField(
        max_digits=QUANTITY_DIGITS,
        decimal_places=QUANTITY_DECIMALS,
        validators=[MinValueValidator(Decimal("0.001"))],
        help_text="How much of this material one batch uses.",
    )

    class Meta:
        ordering = ["material__name"]
        constraints = [
            models.UniqueConstraint(
                fields=["recipe", "material"], name="recipe_material_once"
            ),
            models.CheckConstraint(
                check=models.Q(quantity__gt=0), name="recipe_item_quantity_positive"
            ),
        ]

    def __str__(self):
        return f"{self.quantity} {self.material.unit} {self.material.name}"

    @property
    def line_cost(self) -> Decimal:
        return (self.quantity * self.material.unit_cost).quantize(Decimal("0.01"))


class ProductionStatus(models.TextChoices):
    COMPLETED = "COMPLETED", "Completed"
    REVERSED = "REVERSED", "Reversed"


class ProductionRunQuerySet(models.QuerySet):
    def completed(self):
        return self.filter(status=ProductionStatus.COMPLETED)


class ProductionRun(AuthoredModel, OwnedModel):
    """
    One batch: what came out, what went in, and what it cost.

    Recorded in a single step rather than started-then-finished. A yard reports
    at the end of a shift, and a two-stage flow would leave half-open batches
    nobody closes - the classic way a stock system stops matching the yard.

    Reversal, not deletion. A run that never happened still moved stock in the
    ledger, and the only honest correction is an opposite set of movements with
    both halves on record.
    """

    reference = models.CharField(
        max_length=40, unique=True, db_index=True, editable=False
    )
    product = models.ForeignKey(
        "inventory.Product", on_delete=models.PROTECT, related_name="production_runs"
    )
    quantity_produced = models.PositiveIntegerField(
        help_text="Good units that went into finished stock."
    )
    quantity_rejected = models.PositiveIntegerField(
        default=0,
        help_text="Units that failed. Materials were still consumed for them.",
    )
    produced_on = models.DateField(db_index=True)

    material_cost = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        default=Decimal("0.00"),
        help_text="Sum of the materials consumed, at the cost of the day.",
    )
    unit_cost = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=Decimal("0.00"),
        help_text="material_cost divided by the good units produced.",
    )

    status = models.CharField(
        max_length=10,
        choices=ProductionStatus.choices,
        default=ProductionStatus.COMPLETED,
        db_index=True,
    )
    notes = models.TextField(blank=True)
    note_tag = note_tag_field()

    reversed_at = models.DateTimeField(null=True, blank=True)
    reversed_by = models.ForeignKey(
        "accounts.User",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="production_reversals",
    )
    reversal_reason = models.CharField(max_length=255, blank=True)

    objects = ProductionRunQuerySet.as_manager()

    class Meta:
        ordering = ["-produced_on", "-created_at", "-id"]
        verbose_name = "Production run"
        indexes = [
            models.Index(fields=["product", "-produced_on"]),
            models.Index(fields=["owner", "-produced_on"]),
            models.Index(fields=["status", "-produced_on"]),
        ]

    def __str__(self):
        return self.reference

    def get_absolute_url(self):
        return reverse("production:run_detail", args=[self.pk])

    def save(self, *args, **kwargs):
        if not self.reference:
            from core.utils import generate_reference

            self.reference = generate_reference("PRD", ProductionRun)
        super().save(*args, **kwargs)

    # -- Derived ------------------------------------------------------------
    @property
    def is_reversed(self) -> bool:
        return self.status == ProductionStatus.REVERSED

    @property
    def total_attempted(self) -> int:
        return self.quantity_produced + self.quantity_rejected

    @property
    def yield_percent(self) -> Decimal:
        """Good units as a share of everything attempted."""
        attempted = self.total_attempted
        if not attempted:
            return Decimal("0.00")
        return (
            Decimal(self.quantity_produced) * 100 / Decimal(attempted)
        ).quantize(Decimal("0.01"))

    @property
    def rejected_cost(self) -> Decimal:
        """
        What the damage actually cost, valued at what a GOOD unit costs.

        THIS IS NOT THE OBVIOUS SUM, AND THE DIFFERENCE IS REAL MONEY
        ------------------------------------------------------------
        Pour a mix for 100 blocks, get 80 good and 20 broken, and spend 8,000
        on materials. There are two ways to price the 20:

            across everything attempted   8,000 x 20/100 = 1,600
            at what a good block costs    (8,000/80) x 20 = 2,000

        The first is what a system reports when it forgets that the 20 broken
        blocks cannot be sold. But the 8,000 has to come back out of 80
        blocks, not 100 - each survivor now carries 100, not 80 - and the
        breakage therefore cost 2,000 of sellable product, not 1,600.

        Under-reporting it by 400 a batch is exactly how a yard loses money it
        never sees on a report. `unit_cost` is already material_cost divided
        by the good units, so this multiplies by that, deliberately.
        """
        if not self.quantity_rejected:
            return Decimal("0.00")
        unit = self.unit_cost
        if unit <= 0:
            # A run costed before the unit figure was written (or with no
            # material cost at all). Fall back to deriving it the same way.
            if not self.quantity_produced:
                return Decimal("0.00")
            unit = (self.material_cost / Decimal(self.quantity_produced))
        return (unit * Decimal(self.quantity_rejected)).quantize(Decimal("0.01"))

    @property
    def good_unit_cost(self) -> Decimal:
        """Readable alias: what one sellable unit of this batch cost to make."""
        return self.unit_cost

    @property
    def naive_rejected_cost(self) -> Decimal:
        """
        The same failures priced across every unit attempted.

        Kept only so the run page can show the two side by side - seeing
        "1,600 if you count the broken ones as production, 2,000 in blocks you
        can actually sell" is what makes the rule land for the person reading
        it.
        """
        attempted = self.total_attempted
        if not attempted or not self.quantity_rejected:
            return Decimal("0.00")
        share = Decimal(self.quantity_rejected) / Decimal(attempted)
        return (self.material_cost * share).quantize(Decimal("0.01"))

    @property
    def damage_loss_gap(self) -> Decimal:
        """How much the naive figure under-reports this batch's breakage by."""
        return (self.rejected_cost - self.naive_rejected_cost).quantize(
            Decimal("0.01")
        )

    @property
    def damage_summary(self) -> str:
        """'12 Broken, 8 Cracked' - one line for a list row."""
        parts = [
            f"{d.quantity} {d.type_name}"
            for d in self.damages.all()
            if d.quantity
        ]
        return ", ".join(parts)


class ProductionMaterial(models.Model):
    """
    One material line of a run: what was actually used, at what cost.

    `unit_cost` is copied rather than read through to the material, because
    the cost of cement changes and last month's batch must keep costing what
    last month's cement cost.
    """

    run = models.ForeignKey(
        ProductionRun, on_delete=models.CASCADE, related_name="materials"
    )
    material = models.ForeignKey(
        RawMaterial, on_delete=models.PROTECT, related_name="production_lines"
    )
    quantity = models.DecimalField(
        max_digits=QUANTITY_DIGITS, decimal_places=QUANTITY_DECIMALS
    )
    unit_cost = models.DecimalField(
        max_digits=12, decimal_places=2, default=Decimal("0.00")
    )
    #: What the recipe said this run should have used. Null when the run had
    #: no recipe to compare against.
    expected_quantity = models.DecimalField(
        max_digits=QUANTITY_DIGITS,
        decimal_places=QUANTITY_DECIMALS,
        null=True,
        blank=True,
    )

    class Meta:
        ordering = ["material__name"]
        constraints = [
            models.UniqueConstraint(
                fields=["run", "material"], name="production_material_once"
            ),
        ]

    def __str__(self):
        return f"{self.quantity} {self.material.unit} {self.material.name}"

    @property
    def line_cost(self) -> Decimal:
        return (self.quantity * self.unit_cost).quantize(Decimal("0.01"))

    @property
    def variance(self) -> Decimal | None:
        """Actual minus expected. Positive means the batch over-ran."""
        if self.expected_quantity is None:
            return None
        return self.quantity - self.expected_quantity


class ProductionDamage(models.Model):
    """
    One kind of failure on a batch: what went wrong, and how many.

    WHY THIS IS A LIST AND NOT A NUMBER
    -----------------------------------
    A shift does not fail in one way. Twelve came out cracked because the mix
    was wet, eight got chipped being stacked, three were the wrong size. A
    single 'rejected: 23' box records the loss and throws away every reason
    for it - and the reason is the only part anybody can act on. Split into
    lines, the same data answers 'what keeps breaking our blocks' over a month.

    `ProductionRun.quantity_rejected` stays as the total, kept in step by
    production.services. It is what every cost figure and report already
    reads, and a second source of truth for one number is how the two stop
    agreeing.

    `type_name` is a snapshot beside the link, for the reason given on
    core.models.Option: removing a damage type next year must not blank out
    what last year's batch said broke.
    """

    run = models.ForeignKey(
        ProductionRun, on_delete=models.CASCADE, related_name="damages"
    )
    damage_type = models.ForeignKey(
        "core.Option",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="production_damages",
        help_text="An entry from the DAMAGE_TYPE list.",
    )
    type_name = models.CharField(
        max_length=120,
        blank=True,
        db_index=True,
        help_text="What it was called at the time. Survives the option going.",
    )
    quantity = models.PositiveIntegerField(
        default=0, help_text="How many units failed this way."
    )
    note = models.CharField(max_length=255, blank=True)

    class Meta:
        ordering = ["-quantity", "id"]
        verbose_name = "Production damage"
        verbose_name_plural = "Production damage"
        indexes = [
            models.Index(fields=["run"], name="damage_run_idx"),
            models.Index(fields=["type_name"], name="damage_type_name_idx"),
        ]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(quantity__gt=0), name="damage_quantity_positive"
            ),
        ]

    def __str__(self):
        return f"{self.quantity} x {self.type_name or 'Damaged'}"

    def save(self, *args, **kwargs):
        if self.damage_type_id and not self.type_name:
            self.type_name = self.damage_type.label
        super().save(*args, **kwargs)

    @property
    def unit_cost(self) -> Decimal:
        """What one of these would have cost had it survived."""
        return self.run.unit_cost if self.run_id else Decimal("0.00")

    @property
    def line_cost(self) -> Decimal:
        """
        This line's share of the breakage, at the good-unit cost.

        Same rule as ProductionRun.rejected_cost, and for the same reason:
        broken blocks are paid for by the ones that can still be sold.
        """
        return (self.unit_cost * Decimal(self.quantity)).quantize(Decimal("0.01"))


class RequestStatus(models.TextChoices):
    PENDING = "PENDING", "Waiting for an answer"
    ACCEPTED = "ACCEPTED", "Accepted - will be produced"
    DECLINED = "DECLINED", "Declined"
    FULFILLED = "FULFILLED", "Produced and added to stock"
    CANCELLED = "CANCELLED", "Cancelled by the person who asked"


class ProductionRequestQuerySet(models.QuerySet):
    def open(self):
        return self.filter(
            status__in=[RequestStatus.PENDING, RequestStatus.ACCEPTED]
        )

    def pending(self):
        return self.filter(status=RequestStatus.PENDING)

    def for_decider(self, user):
        return self.filter(assigned_to=user)


class ProductionRequest(TimeStampedModel):
    """
    'We are running out of this - please make more.'

    WHY A RECORD AND NOT A PHONE CALL
    ---------------------------------
    The automatic low-stock alert (api/signals.py) tells the owner a product
    is nearly gone. It does not say how many are wanted, who is waiting, or
    whether anybody agreed to make them - so it gets read, half-remembered,
    and the seller finds out at the counter that nothing was poured.

    A request is the missing half: a named person asked a named person for a
    stated quantity, and that person answered. It survives the notification
    being swiped away, and it is the only way "I told them last week" can be
    checked rather than argued about.

    `stock_at_request` is a snapshot. By the time the manager reads it the
    shelf has moved, and the interesting number is what it looked like when
    somebody thought it was worth asking.
    """

    product = models.ForeignKey(
        "inventory.Product",
        on_delete=models.CASCADE,
        related_name="production_requests",
    )
    quantity = models.PositiveIntegerField(
        help_text="How many units are being asked for."
    )

    requested_by = models.ForeignKey(
        "accounts.User",
        on_delete=models.CASCADE,
        related_name="production_requests_made",
    )
    assigned_to = models.ForeignKey(
        "accounts.User",
        on_delete=models.CASCADE,
        related_name="production_requests_received",
        help_text="The manager or administrator being asked.",
    )

    reason = models.ForeignKey(
        "core.Option",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="production_requests",
        help_text="An entry from the PRODUCTION_REQUEST_REASON list.",
    )
    reason_name = models.CharField(max_length=120, blank=True)
    note = models.CharField(max_length=255, blank=True)
    note_tag = note_tag_field()

    needed_by = models.DateField(null=True, blank=True)
    stock_at_request = models.IntegerField(
        default=0, help_text="What the shelf held when this was raised."
    )

    status = models.CharField(
        max_length=10,
        choices=RequestStatus.choices,
        default=RequestStatus.PENDING,
        db_index=True,
    )
    responded_at = models.DateTimeField(null=True, blank=True)
    response_note = models.CharField(max_length=255, blank=True)
    fulfilled_run = models.ForeignKey(
        ProductionRun,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="requests_fulfilled",
    )

    objects = ProductionRequestQuerySet.as_manager()

    class Meta:
        ordering = ["-created_at", "-id"]
        verbose_name = "Production request"
        indexes = [
            models.Index(
                fields=["assigned_to", "status", "-created_at"],
                name="request_assigned_idx",
            ),
            models.Index(
                fields=["requested_by", "-created_at"], name="request_asker_idx"
            ),
            models.Index(fields=["product", "status"], name="request_product_idx"),
        ]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(quantity__gt=0), name="request_quantity_positive"
            ),
        ]

    def __str__(self):
        return f"{self.quantity} x {self.product.name} ({self.get_status_display()})"

    def get_absolute_url(self):
        return reverse("production:request_list")

    @property
    def is_open(self) -> bool:
        return self.status in {RequestStatus.PENDING, RequestStatus.ACCEPTED}

    @property
    def is_pending(self) -> bool:
        return self.status == RequestStatus.PENDING

    @property
    def status_class(self) -> str:
        return {
            RequestStatus.PENDING: "warning",
            RequestStatus.ACCEPTED: "info",
            RequestStatus.DECLINED: "danger",
            RequestStatus.FULFILLED: "success",
            RequestStatus.CANCELLED: "secondary",
        }.get(self.status, "secondary")

    @property
    def is_overdue(self) -> bool:
        from django.utils import timezone

        return bool(
            self.needed_by and self.is_open and self.needed_by < timezone.localdate()
        )
