"""
Putting right a delivery that was entered wrong.

THE PROBLEM
-----------
Somebody types 48 bags where 4.8 were delivered, or records a delivery that
never came at all. The Audit's money out then says the owner spent 180,000 he
never spent - and the ledger cannot simply be edited: a stock card anybody can
rewrite proves nothing, so movements are append-only (production.MaterialMovement,
inventory.StockMovement).

WHAT A CORRECTION IS
--------------------
The row keeps what was typed and gains what it SHOULD have been: the quantity
really delivered (0 when it never happened) and the real cost per unit, with
who corrected it, when and why. The Audit counts the money from the corrected
figures; the original stays on record and in the audit log.

THE STORE IS A SEPARATE QUESTION
--------------------------------
A wrong delivery also put goods on the stock card that are not on the shelf.
Very often somebody has already noticed and counted the store, so the count
has put the stock right - and taking the goods off again would leave the card
below zero. So a correction changes the stock only when asked
(`change_stock`), by posting an ordinary correcting ADJUSTMENT for the
difference, and refuses a change that would take the store below zero.

The material's (or product's) current cost is refreshed too when this was the
delivery that set it and nothing has changed it since - a wrong price typed on
the last delivery would otherwise go on pricing every batch.
"""
from decimal import Decimal, InvalidOperation

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from core.utils import money

#: The kinds of ledger row a correction applies to, and what it takes to
#: correct one: a delivery of raw material, or finished goods bought in.
SOURCES = {
    "material": {"permission": "material.adjust", "movement_type": "PURCHASE"},
    "product": {"permission": "stock.adjust", "movement_type": "RESTOCK"},
}


class CorrectionError(ValueError):
    """A correction that cannot be made. The message is safe to show."""


def _ledger(source):
    if source == "material":
        from production.models import MaterialMovement

        return MaterialMovement
    if source == "product":
        from inventory.models import StockMovement

        return StockMovement
    raise CorrectionError("Only deliveries and restocks can be corrected.")


def can_correct(user, source) -> bool:
    spec = SOURCES.get(source)
    return bool(spec and user is not None and user.has_access(spec["permission"]))


def _parse_quantity(raw, source):
    try:
        value = Decimal(str(raw).replace(",", "").strip())
    except (InvalidOperation, ValueError, TypeError):
        raise CorrectionError("Enter the quantity as a number, for example 12.")
    if not value.is_finite() or value < 0:
        raise CorrectionError("The quantity cannot be below zero.")
    if source == "product":
        if value != value.to_integral_value():
            raise CorrectionError("Products are counted in whole units.")
        return int(value)
    return value.quantize(Decimal("0.001"))


def _parse_cost(raw):
    if raw in (None, ""):
        return None
    try:
        value = Decimal(str(raw).replace(",", "").strip())
    except (InvalidOperation, ValueError, TypeError):
        raise CorrectionError("Enter the cost as a number, for example 24.50.")
    if not value.is_finite() or value < 0:
        raise CorrectionError("A cost cannot be below zero.")
    if value > Decimal("9999999999.99"):
        raise CorrectionError("That cost is too large.")
    return money(value)


def correct_delivery(source, pk, *, user, quantity, unit_cost=None, note="",
                     change_stock=False, request=None):
    """
    Correct one delivery (source "material") or restock (source "product").

    `quantity` is what was really delivered - 0 when it never happened.
    `unit_cost` is the real cost per unit; blank keeps the one counted now.
    `change_stock` also moves the store by the difference.

    Returns the corrected ledger row.
    """
    from accounts.models import AuditAction
    from accounts.services import log_action
    from core.scoping import scoped

    spec = SOURCES.get(source)
    if spec is None:
        raise CorrectionError("Only deliveries and restocks can be corrected.")
    if not can_correct(user, source):
        raise CorrectionError("You may not correct deliveries.")
    Ledger = _ledger(source)
    new_quantity = _parse_quantity(quantity, source)
    new_cost = _parse_cost(unit_cost)
    note = (note or "").strip()[:255]

    with transaction.atomic():
        try:
            row = Ledger.objects.select_for_update().get(pk=pk)
        except Ledger.DoesNotExist:
            raise CorrectionError("That delivery was not found.")
        item = row.material if source == "material" else row.product
        # The same rule as everywhere else: what this person may not see, he
        # may not correct.
        if not scoped(type(item).objects.filter(pk=item.pk), user).exists():
            raise CorrectionError("That delivery was not found.")
        if row.movement_type != spec["movement_type"] or row.quantity_delta <= 0:
            raise CorrectionError("Only deliveries and restocks can be corrected.")

        was_quantity = row.counted_quantity
        was_cost = row.counted_unit_cost
        if new_cost is None:
            new_cost = was_cost

        if change_stock and new_quantity != was_quantity:
            difference = new_quantity - was_quantity
            when = timezone.localtime(row.created_at).strftime("%d %b %Y")
            reason = f"Delivery of {when} corrected: {was_quantity} -> {new_quantity}"
            try:
                if source == "material":
                    from production.models import MaterialMovementType
                    from production.services import apply_material_movement

                    apply_material_movement(
                        item, difference, MaterialMovementType.ADJUSTMENT,
                        user=user, reason=reason, reference=f"FIX-{row.pk}",
                        allow_negative=False,
                    )
                else:
                    from inventory.models import MovementType
                    from inventory.services import apply_stock_movement

                    apply_stock_movement(
                        item, difference, MovementType.ADJUSTMENT,
                        user=user, reason=reason, reference=f"FIX-{row.pk}",
                        allow_negative=False,
                    )
            except ValidationError as exc:
                text = " ".join(getattr(exc, "messages", [str(exc)]))
                raise CorrectionError(
                    f"{text} If the store has been counted since, leave the stock as it is."
                )

        back_to_original = new_quantity == row.quantity_delta and new_cost == row.unit_cost
        if back_to_original:
            row.corrected_quantity = row.corrected_unit_cost = None
            row.corrected_at = row.corrected_by = None
            row.correction_note = ""
        else:
            row.corrected_quantity = new_quantity
            row.corrected_unit_cost = new_cost
            row.corrected_at = timezone.now()
            row.corrected_by = user
            row.correction_note = note
        row.save(update_fields=[
            "corrected_quantity", "corrected_unit_cost", "corrected_at",
            "corrected_by", "correction_note",
        ])

        # A wrong price on the delivery that set the item's current cost
        # would go on pricing everything; put it right too - but only when
        # nothing has changed that cost since.
        _refresh_cost(source, row, item, was_cost, new_cost)

        unit = item.get_unit_display()
        if new_quantity == 0:
            text = (f"Delivery of {row.quantity_delta} {unit} of {item.name} "
                    f"marked as never happened.")
        else:
            text = (f"Delivery of {item.name} corrected from {was_quantity} x {was_cost} "
                    f"to {new_quantity} x {new_cost}.")
        if note:
            text += f" Note: {note}"
        log_action(
            AuditAction.UPDATE, instance=item, description=text,
            changes={
                "delivery": str(row.pk),
                "quantity": {"from": str(was_quantity), "to": str(new_quantity)},
                "unit_cost": {"from": str(was_cost), "to": str(new_cost)},
                "stock_changed": bool(change_stock and new_quantity != was_quantity),
            },
            user=user, request=request,
        )
    return row


def _refresh_cost(source, row, item, was_cost, new_cost):
    if new_cost is None or was_cost is None or new_cost == was_cost or new_cost <= 0:
        return
    Ledger = type(row)
    later = Ledger.objects.filter(
        **{source: item}, movement_type=row.movement_type,
        created_at__gt=row.created_at,
    ).exists()
    if later:
        return
    if source == "material":
        if item.unit_cost == was_cost:
            type(item).objects.filter(pk=item.pk).update(unit_cost=new_cost)
    elif item.cost_price == was_cost:
        type(item).objects.filter(pk=item.pk).update(cost_price=new_cost)
