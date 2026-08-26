"""
The single doorway through which stock quantities may change.

Nothing anywhere else in the codebase should ever write to
Product.stock_quantity directly. Doing so breaks the guarantee that
SUM(StockMovement.quantity_delta) == Product.stock_quantity.
"""
import logging
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import transaction

from core.scoping import can_touch

from .models import MovementType, Product, StockMovement

logger = logging.getLogger(__name__)


class InsufficientStockError(ValidationError):
    """Raised when a sale would push stock below zero on a product that forbids it."""


@transaction.atomic
def apply_stock_movement(
    product,
    quantity_delta: int,
    movement_type: str,
    *,
    user=None,
    reference: str = "",
    reason: str = "",
    unit_cost: Decimal | None = None,
    unit_price: Decimal | None = None,
    allow_negative: bool | None = None,
) -> StockMovement:
    """
    Atomically move stock and write the ledger row.

    The row is locked with SELECT ... FOR UPDATE so two concurrent sales of
    the last unit cannot both succeed.
    """
    if quantity_delta == 0:
        raise ValidationError("Stock movement quantity cannot be zero.")

    # The last gate before the ledger. Every stock change in the system funnels
    # through here, so one check covers restock, sale, adjustment and write-off
    # at once. `user=None` means a system task (reconciliation, migrations),
    # which is trusted by definition and skipped.
    if user is not None and not can_touch(product, user):
        raise ValidationError(
            f"'{product.name}' is not in your product list."
        )

    # Lock this product row for the duration of the transaction.
    locked = Product.objects.select_for_update().get(pk=product.pk)

    before = locked.stock_quantity
    after = before + quantity_delta

    permit_negative = (
        locked.allow_negative_stock if allow_negative is None else allow_negative
    )
    if after < 0 and not permit_negative:
        raise InsufficientStockError(
            f"Insufficient stock for '{locked.name}'. "
            f"Available: {before}, requested: {abs(quantity_delta)}."
        )

    movement = StockMovement.objects.create(
        product=locked,
        movement_type=movement_type,
        quantity_delta=quantity_delta,
        quantity_before=before,
        quantity_after=after,
        unit_cost=unit_cost,
        unit_price=unit_price,
        reference=reference,
        reason=reason,
        performed_by=user,
    )

    Product.objects.filter(pk=locked.pk).update(stock_quantity=after)
    product.stock_quantity = after  # keep the caller's instance honest

    logger.info(
        "Stock %s %+d for %s (%s -> %s) ref=%s",
        movement_type,
        quantity_delta,
        locked.sku,
        before,
        after,
        reference or "-",
    )
    return movement


def restock(product, quantity, *, user=None, unit_cost=None, reference="", reason=""):
    if quantity <= 0:
        raise ValidationError("Restock quantity must be positive.")
    movement = apply_stock_movement(
        product,
        quantity,
        MovementType.RESTOCK,
        user=user,
        unit_cost=unit_cost,
        reference=reference,
        reason=reason or "Stock received",
    )
    # A restock is the natural moment to refresh the cost price.
    if unit_cost is not None and unit_cost > 0:
        Product.objects.filter(pk=product.pk).update(cost_price=unit_cost)
        product.cost_price = unit_cost
    return movement


def deduct_for_sale(product, quantity, *, user=None, reference="", unit_price=None):
    if quantity <= 0:
        raise ValidationError("Sale quantity must be positive.")
    return apply_stock_movement(
        product,
        -quantity,
        MovementType.SALE,
        user=user,
        reference=reference,
        unit_price=unit_price,
        reason="Sold",
    )


def return_from_customer(product, quantity, *, user=None, reference="", reason=""):
    if quantity <= 0:
        raise ValidationError("Return quantity must be positive.")
    return apply_stock_movement(
        product,
        quantity,
        MovementType.RETURN_IN,
        user=user,
        reference=reference,
        reason=reason or "Customer return",
    )


def reverse_sale(product, quantity, *, user=None, reference="", reason=""):
    """Used when an Admin voids a transaction - puts the goods back."""
    return apply_stock_movement(
        product,
        quantity,
        MovementType.VOID_REVERSAL,
        user=user,
        reference=reference,
        reason=reason or "Sale voided",
    )


def adjust_to(product, new_quantity: int, *, user=None, reason=""):
    """
    Set stock to an absolute counted figure (stock-take).
    Writes the *difference* as an ADJUSTMENT row.
    """
    locked = Product.objects.select_for_update().get(pk=product.pk)
    delta = new_quantity - locked.stock_quantity
    if delta == 0:
        return None
    return apply_stock_movement(
        locked,
        delta,
        MovementType.ADJUSTMENT,
        user=user,
        reason=reason or "Stock count adjustment",
        allow_negative=True,
    )


def write_off(product, quantity, *, user=None, reason=""):
    if quantity <= 0:
        raise ValidationError("Write-off quantity must be positive.")
    return apply_stock_movement(
        product,
        -quantity,
        MovementType.DAMAGE,
        user=user,
        reason=reason or "Damaged / written off",
        allow_negative=True,
    )


def reconcile_all_products() -> dict:
    """
    Maintenance task: replay the ledger for every product and report drift.
    Wire this to a management command or a nightly cron.
    """
    drifted = {}
    for product in Product.objects.all():
        cached = product.stock_quantity
        actual = product.recalculate_stock_from_ledger()
        if cached != actual:
            drifted[product.sku] = {"cached": cached, "corrected": actual}
            logger.warning("Stock drift on %s: %s -> %s", product.sku, cached, actual)
    return drifted
