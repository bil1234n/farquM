"""
The single doorway through which material quantities and production may change.

Nothing anywhere else may write to RawMaterial.quantity_in_stock. Doing so
breaks the guarantee that

    SUM(MaterialMovement.quantity_delta) == RawMaterial.quantity_in_stock

which is the only reason the store card is worth reading.

Production sits here too, and not in a view, because recording a batch is four
writes that must all happen or none of them: consume every material, add the
finished goods, cost the batch, and stamp the product's cost price. A run that
half-happened would leave cement missing with no blocks to show for it.
"""
import logging
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from core.scoping import can_touch
from inventory.models import MovementType, Product
from inventory.services import apply_stock_movement

from .models import (
    MaterialMovement,
    MaterialMovementType,
    ProductionMaterial,
    ProductionRun,
    ProductionStatus,
    RawMaterial,
    Recipe,
)

logger = logging.getLogger(__name__)

MONEY = Decimal("0.01")
QUANTITY = Decimal("0.001")


class InsufficientMaterialError(ValidationError):
    """Raised when a run would push a material below zero."""


def _quantise(value) -> Decimal:
    return Decimal(str(value)).quantize(QUANTITY)


# ---------------------------------------------------------------------------
# The ledger
# ---------------------------------------------------------------------------
@transaction.atomic
def apply_material_movement(
    material,
    quantity_delta,
    movement_type: str,
    *,
    user=None,
    reference: str = "",
    reason: str = "",
    unit_cost: Decimal | None = None,
    allow_negative: bool | None = None,
) -> MaterialMovement:
    """
    Atomically move material and write the ledger row.

    The row is locked with SELECT ... FOR UPDATE so two shifts drawing the last
    of the cement cannot both succeed.
    """
    quantity_delta = _quantise(quantity_delta)
    if quantity_delta == 0:
        raise ValidationError("A material movement cannot be zero.")

    # The last gate before the ledger. Every change funnels through here, so
    # one check covers deliveries, production, waste and stock counts at once.
    # `user=None` means a system task - a migration or reconciliation - which
    # is trusted by definition.
    if user is not None and not can_touch(material, user):
        raise ValidationError(f"'{material.name}' is not in your material store.")

    locked = RawMaterial.objects.select_for_update().get(pk=material.pk)

    before = locked.quantity_in_stock
    after = before + quantity_delta

    permit_negative = (
        locked.allow_negative_stock if allow_negative is None else allow_negative
    )
    if after < 0 and not permit_negative:
        raise InsufficientMaterialError(
            f"Not enough '{locked.name}'. Available: {before} {locked.unit}, "
            f"needed: {abs(quantity_delta)} {locked.unit}."
        )

    movement = MaterialMovement.objects.create(
        material=locked,
        movement_type=movement_type,
        quantity_delta=quantity_delta,
        quantity_before=before,
        quantity_after=after,
        unit_cost=unit_cost if unit_cost is not None else locked.unit_cost,
        reference=reference,
        reason=reason,
        performed_by=user,
    )

    RawMaterial.objects.filter(pk=locked.pk).update(quantity_in_stock=after)
    material.quantity_in_stock = after  # keep the caller's instance honest

    logger.info(
        "Material %s %+s for %s (%s -> %s) ref=%s",
        movement_type,
        quantity_delta,
        locked.code,
        before,
        after,
        reference or "-",
    )
    return movement


def receive_material(
    material, quantity, *, user=None, unit_cost=None, reference="", reason=""
):
    """A delivery arrives."""
    quantity = _quantise(quantity)
    if quantity <= 0:
        raise ValidationError("A delivery quantity must be greater than zero.")
    movement = apply_material_movement(
        material,
        quantity,
        MaterialMovementType.PURCHASE,
        user=user,
        unit_cost=unit_cost,
        reference=reference,
        reason=reason or "Delivery received",
    )
    # A delivery is the natural moment to refresh what the material costs.
    # Everything priced afterwards uses the new figure; everything already
    # costed keeps the old one, because those lines copied it.
    if unit_cost is not None and Decimal(unit_cost) > 0:
        RawMaterial.objects.filter(pk=material.pk).update(unit_cost=unit_cost)
        material.unit_cost = unit_cost
    return movement


def waste_material(material, quantity, *, user=None, reason="", reference=""):
    """Spoiled, spilled, or set solid in the bag."""
    quantity = _quantise(quantity)
    if quantity <= 0:
        raise ValidationError("A write-off quantity must be greater than zero.")
    return apply_material_movement(
        material,
        -quantity,
        MaterialMovementType.WASTE,
        user=user,
        reference=reference,
        reason=reason or "Spoiled / written off",
        allow_negative=True,
    )


def return_material_to_supplier(material, quantity, *, user=None, reason="",
                                reference=""):
    quantity = _quantise(quantity)
    if quantity <= 0:
        raise ValidationError("A return quantity must be greater than zero.")
    return apply_material_movement(
        material,
        -quantity,
        MaterialMovementType.RETURN_OUT,
        user=user,
        reference=reference,
        reason=reason or "Returned to supplier",
    )


def recount_material(material, counted_quantity, *, user=None, reason=""):
    """
    Set a material to a counted figure, writing the difference as a correction.

    Returns None when the count already matches, which is not an error and not
    worth a ledger row.
    """
    counted = _quantise(counted_quantity)
    if counted < 0:
        raise ValidationError("A counted quantity cannot be negative.")

    locked = RawMaterial.objects.select_for_update().get(pk=material.pk)
    delta = counted - locked.quantity_in_stock
    if delta == 0:
        return None
    return apply_material_movement(
        locked,
        delta,
        MaterialMovementType.ADJUSTMENT,
        user=user,
        reason=reason or "Stock count correction",
        allow_negative=True,
    )


# ---------------------------------------------------------------------------
# Planning
# ---------------------------------------------------------------------------
def plan_for(product, quantity_attempted: int, *, user=None) -> dict:
    """
    What making `quantity_attempted` units of `product` would take.

    Answers before anything is written, so the operator sees the shortage while
    they can still do something about it. `quantity_attempted` is the whole
    mix - good units plus the ones expected to fail - because the materials go
    in before anyone knows which is which.

    Returns a dict the web form and the phone both render from, so the two can
    never disagree about what a batch needs.
    """
    if quantity_attempted <= 0:
        raise ValidationError("Enter how many units this run will make.")

    recipe = Recipe.objects.filter(product=product, is_active=True).first()
    lines = []
    shortages = []
    total_cost = Decimal("0.00")

    if recipe is not None and recipe.output_quantity:
        scale = Decimal(quantity_attempted) / Decimal(recipe.output_quantity)
        items = recipe.items.select_related("material").all()
        for item in items:
            material = item.material
            needed = _quantise(item.quantity * scale)
            available = material.quantity_in_stock
            short = needed - available
            line_cost = (needed * material.unit_cost).quantize(MONEY)
            total_cost += line_cost
            lines.append(
                {
                    "material": material,
                    "material_id": material.pk,
                    "material_name": material.name,
                    "material_code": material.code,
                    "unit": material.unit,
                    "unit_display": material.get_unit_display(),
                    "required": needed,
                    "available": available,
                    "unit_cost": material.unit_cost,
                    "line_cost": line_cost,
                    "is_short": short > 0,
                    "short_by": _quantise(short) if short > 0 else ZERO_QUANTITY,
                }
            )
            if short > 0:
                shortages.append(material.name)

    return {
        "product": product,
        "quantity": quantity_attempted,
        "has_recipe": recipe is not None,
        "recipe": recipe,
        "output_quantity": recipe.output_quantity if recipe else None,
        "lines": lines,
        "material_cost": total_cost.quantize(MONEY),
        "unit_cost": (total_cost / quantity_attempted).quantize(MONEY)
        if quantity_attempted
        else Decimal("0.00"),
        "shortages": shortages,
        "can_produce": not shortages,
    }


ZERO_QUANTITY = Decimal("0.000")


# ---------------------------------------------------------------------------
# Production
# ---------------------------------------------------------------------------
@transaction.atomic
def record_production(
    *,
    product,
    quantity_produced: int,
    materials,
    user,
    quantity_rejected: int = 0,
    produced_on=None,
    notes: str = "",
    update_product_cost: bool = True,
) -> ProductionRun:
    """
    Record one finished batch.

    `materials` is a list of {"material": RawMaterial | pk, "quantity": Decimal}
    describing what was ACTUALLY used - not what the recipe said. The recipe is
    a starting point the operator corrects; storing the corrected figure next
    to the expected one is the whole reason a yard can find out where its
    cement goes.

    Every write is inside one transaction. If the last material is short, the
    blocks are not created either.
    """
    if quantity_produced <= 0:
        raise ValidationError("A run must produce at least one unit.")
    if quantity_rejected < 0:
        raise ValidationError("Rejected units cannot be negative.")
    if not materials:
        raise ValidationError("Record at least one material used in this run.")

    if not can_touch(product, user):
        raise ValidationError(f"'{product.name}' is not in your product list.")

    produced_on = produced_on or timezone.localdate()

    # Resolve and validate every line before a single row is written, so a
    # typo in the last line does not leave the first three consumed.
    resolved = []
    seen = set()
    for line in materials:
        raw = line.get("material")
        material = (
            raw
            if isinstance(raw, RawMaterial)
            else RawMaterial.objects.filter(pk=raw).first()
        )
        if material is None:
            raise ValidationError("One of the materials no longer exists.")
        if material.pk in seen:
            raise ValidationError(
                f"'{material.name}' is listed twice. Combine it into one line."
            )
        seen.add(material.pk)

        quantity = _quantise(line.get("quantity") or 0)
        if quantity <= 0:
            raise ValidationError(
                f"Enter how much '{material.name}' this run used."
            )
        if not can_touch(material, user):
            raise ValidationError(
                f"'{material.name}' is not in your material store."
            )
        expected = line.get("expected_quantity")
        resolved.append(
            {
                "material": material,
                "quantity": quantity,
                "expected": _quantise(expected) if expected is not None else None,
            }
        )

    run = ProductionRun.objects.create(
        product=product,
        quantity_produced=quantity_produced,
        quantity_rejected=quantity_rejected,
        produced_on=produced_on,
        notes=notes,
        # The run belongs to whoever owns the shelf it fills, not to whoever
        # typed it in. That keeps a batch and the blocks it made on the same
        # side of every scoping rule.
        owner=product.owner or user,
        created_by=user,
        updated_by=user,
    )

    total_cost = Decimal("0.00")
    for line in resolved:
        material = line["material"]
        quantity = line["quantity"]
        unit_cost = material.unit_cost

        apply_material_movement(
            material,
            -quantity,
            MaterialMovementType.CONSUMED,
            user=user,
            reference=run.reference,
            reason=f"Used making {product.name}",
            unit_cost=unit_cost,
        )

        ProductionMaterial.objects.create(
            run=run,
            material=material,
            quantity=quantity,
            unit_cost=unit_cost,
            expected_quantity=line["expected"],
        )
        total_cost += (quantity * unit_cost).quantize(MONEY)

    unit_cost = (total_cost / quantity_produced).quantize(MONEY)
    ProductionRun.objects.filter(pk=run.pk).update(
        material_cost=total_cost.quantize(MONEY), unit_cost=unit_cost
    )
    run.material_cost = total_cost.quantize(MONEY)
    run.unit_cost = unit_cost

    # The finished goods. Routed through the inventory service so the product
    # ledger stays the single account of how stock got there.
    apply_stock_movement(
        product,
        quantity_produced,
        MovementType.PRODUCTION,
        user=user,
        reference=run.reference,
        reason=f"Produced in {run.reference}",
        unit_cost=unit_cost,
    )

    # What it actually cost to make is a better cost price than whatever was
    # typed when the product was created. Off by request for a yard that
    # prices from a standard rather than from the last batch.
    if update_product_cost and unit_cost > 0:
        Product.objects.filter(pk=product.pk).update(cost_price=unit_cost)
        product.cost_price = unit_cost

    logger.info(
        "Production %s: %s x%d (rejected %d) cost=%s unit=%s by %s",
        run.reference,
        product.sku,
        quantity_produced,
        quantity_rejected,
        total_cost,
        unit_cost,
        getattr(user, "username", "?"),
    )
    return run


@transaction.atomic
def reverse_production(run, *, user, reason: str = "") -> ProductionRun:
    """
    Undo a run: materials go back on the shelf, finished goods come off it.

    Both halves are written as new movements rather than by deleting the old
    ones. Somebody has to be able to see that a batch was recorded and then
    taken back - that is the difference between a correction and a cover-up.
    """
    if not reason.strip():
        raise ValidationError("Give a reason for reversing this run.")

    locked = ProductionRun.objects.select_for_update().get(pk=run.pk)
    if locked.status == ProductionStatus.REVERSED:
        raise ValidationError(f"{locked.reference} was already reversed.")
    if not can_touch(locked, user):
        raise ValidationError("This production run is not yours to reverse.")

    # Take the blocks back first. If the shelf has already sold them this
    # fails, and it should: the materials must not reappear for goods that
    # left the yard.
    apply_stock_movement(
        locked.product,
        -locked.quantity_produced,
        MovementType.PRODUCTION_REVERSAL,
        user=user,
        reference=locked.reference,
        reason=f"Reversed {locked.reference}: {reason}",
    )

    for line in locked.materials.select_related("material").all():
        apply_material_movement(
            line.material,
            line.quantity,
            MaterialMovementType.PRODUCTION_REVERSAL,
            user=user,
            reference=locked.reference,
            reason=f"Returned by reversal of {locked.reference}",
            unit_cost=line.unit_cost,
        )

    locked.status = ProductionStatus.REVERSED
    locked.reversed_at = timezone.now()
    locked.reversed_by = user
    locked.reversal_reason = reason.strip()[:255]
    locked.updated_by = user
    locked.save(
        update_fields=[
            "status",
            "reversed_at",
            "reversed_by",
            "reversal_reason",
            "updated_by",
            "updated_at",
        ]
    )

    logger.info(
        "Production %s reversed by %s: %s",
        locked.reference,
        getattr(user, "username", "?"),
        reason,
    )
    return locked


# ---------------------------------------------------------------------------
# Maintenance
# ---------------------------------------------------------------------------
def reconcile_all_materials() -> dict:
    """Replay the ledger for every material and report drift."""
    drifted = {}
    for material in RawMaterial.objects.all():
        cached = material.quantity_in_stock
        actual = material.recalculate_from_ledger()
        if cached != actual:
            drifted[material.code] = {"cached": str(cached), "corrected": str(actual)}
            logger.warning(
                "Material drift on %s: %s -> %s", material.code, cached, actual
            )
    return drifted
