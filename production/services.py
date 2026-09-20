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

from core.models import resolve_option
from core.scoping import can_touch
from inventory.models import MovementType, Product
from inventory.services import apply_stock_movement

from .models import (
    MaterialMovement,
    MaterialMovementType,
    ProductionDamage,
    ProductionMaterial,
    ProductionRequest,
    ProductionRun,
    ProductionStatus,
    RawMaterial,
    Recipe,
    RequestStatus,
)

#: The pick-list a damage line chooses from. Named once so the service, the
#: forms and both clients cannot drift onto different lists.
DAMAGE_GROUP = "DAMAGE_TYPE"
REQUEST_REASON_GROUP = "PRODUCTION_REQUEST_REASON"

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


@transaction.atomic
def recount_material(material, counted_quantity, *, user=None, reason=""):
    """
    Set a material to a counted figure, writing the difference as a correction.

    Returns None when the count already matches, which is not an error and not
    worth a ledger row.

    `atomic` is not decoration. The read below takes a row lock, and a lock
    outside a transaction is two separate things wrong at once: Django refuses
    it outright on a real connection, and even where it did not, the lock would
    be released the instant the SELECT returned - so two people counting the
    same bay would both read the same "before" figure and the second write
    would silently erase the first.
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
def normalise_damages(damages, *, user=None) -> list[dict]:
    """
    Clean the damage lines a client sent into rows this module can write.

    Each line may name an existing type by id, or type a new one - the new
    name is added to the shared list on the spot, which is the whole point of
    being able to add from inside the select. Lines naming the same type are
    combined rather than rejected: somebody tapping 'add more damage' twice
    and picking 'Cracked' both times means twenty cracked, not an error
    message about their own form.

    Raises ValidationError on a line with no quantity, because a damage line
    that records nothing is a line somebody meant to fill in.
    """
    if not damages:
        return []

    merged: dict[str, dict] = {}
    for raw in damages:
        quantity = int(raw.get("quantity") or 0)
        type_id = raw.get("damage_type") or raw.get("damage_type_id")
        name = (raw.get("type_name") or raw.get("label") or "").strip()

        if quantity <= 0:
            if not type_id and not name:
                # A blank row left behind by the "add more" button. Ignored,
                # not complained about.
                continue
            raise ValidationError(
                f"Enter how many units were {name or 'damaged'}."
            )
        if not type_id and not name:
            raise ValidationError("Choose what kind of damage this was.")

        option, label = resolve_option(
            DAMAGE_GROUP, label=name, option_id=type_id, user=user
        )
        key = str(option.pk) if option is not None else label.lower()
        line = merged.get(key)
        if line is None:
            merged[key] = {
                "option": option,
                "type_name": label or (option.label if option else ""),
                "quantity": quantity,
                "note": (raw.get("note") or "").strip()[:255],
            }
        else:
            line["quantity"] += quantity
            note = (raw.get("note") or "").strip()
            if note and note not in line["note"]:
                line["note"] = f"{line['note']}; {note}".strip("; ")[:255]

    return list(merged.values())


@transaction.atomic
def record_production(
    *,
    product,
    quantity_produced: int,
    materials,
    user,
    quantity_rejected: int = 0,
    damages=None,
    produced_on=None,
    notes: str = "",
    note_tag=None,
    update_product_cost: bool = True,
    fulfils=None,
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

    # Damage lines, when the form sent any, ARE the rejected figure. Keeping a
    # separate box that could disagree with the lines beneath it is how a run
    # ends up saying 20 broke while listing 23, and no report can then be
    # trusted. The lines win because they are the ones somebody itemised.
    damage_lines = normalise_damages(damages, user=user)
    if damage_lines:
        quantity_rejected = sum(line["quantity"] for line in damage_lines)

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
        note_tag=note_tag,
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

    # Divided by the GOOD units, not by everything attempted. The whole batch
    # was paid for out of the units that can actually be sold, so 8,000 of
    # cement across 80 survivors is 100 each - and the 20 that broke therefore
    # cost 2,000 of sellable product, which is what `rejected_cost` reports.
    # See ProductionRun.rejected_cost for why the other sum loses money.
    unit_cost = (total_cost / quantity_produced).quantize(MONEY)
    ProductionRun.objects.filter(pk=run.pk).update(
        material_cost=total_cost.quantize(MONEY), unit_cost=unit_cost
    )
    run.material_cost = total_cost.quantize(MONEY)
    run.unit_cost = unit_cost

    # What broke, and how. Written after the costing so the lines can be read
    # back at the good-unit price straight away.
    if damage_lines:
        ProductionDamage.objects.bulk_create([
            ProductionDamage(
                run=run,
                damage_type=line["option"],
                type_name=line["type_name"],
                quantity=line["quantity"],
                note=line["note"],
            )
            for line in damage_lines
        ])
        for line in damage_lines:
            if line["option"] is not None:
                line["option"].touch_use()

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

    # Close the loop on whoever asked for this. Done here rather than left to
    # the caller so a batch recorded from the phone closes the request the
    # same way one recorded from the browser does.
    if fulfils:
        _close_requests(fulfils, run=run, user=user)

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
def assignable_deciders(user=None):
    """
    Who a request may be addressed to.

    Anybody who can actually record a batch - that is the only useful test.
    Addressing a request to somebody without `production.create` produces a
    notification they cannot act on, which is worse than no notification at
    all: they assume it is handled and so does the sender.

    Sorted with administrators first, then by name, so the list opens on the
    people most likely to be the right answer.
    """
    from accounts.models import User

    candidates = User.objects.active_staff().select_related()
    people = [
        person
        for person in candidates
        if person.has_access("production.create")
        and person.pk != getattr(user, "pk", None)
    ]
    people.sort(key=lambda p: (0 if p.is_admin else 1, p.display_name.lower()))
    return people


@transaction.atomic
def request_production(
    *,
    product,
    quantity: int,
    requested_by,
    assigned_to,
    reason_id=None,
    reason_name: str = "",
    note: str = "",
    note_tag=None,
    needed_by=None,
) -> ProductionRequest:
    """
    Ask a named person for more of a product, and tell them.

    The notification is sent inside the transaction on purpose - not because
    it must be atomic (push.notify_users swallows its own failures), but so
    that the row and the message are written in one place and cannot drift
    apart as callers multiply.
    """
    if quantity <= 0:
        raise ValidationError("Say how many units you need.")
    if assigned_to is None:
        raise ValidationError("Choose who should make them.")
    if not assigned_to.is_active:
        raise ValidationError(f"{assigned_to.display_name} is no longer active.")
    if not assigned_to.has_access("production.create"):
        raise ValidationError(
            f"{assigned_to.display_name} cannot record production, so they "
            "cannot act on this request."
        )
    if needed_by is not None and needed_by < timezone.localdate():
        raise ValidationError("The date needed cannot be in the past.")

    # A duplicate helps nobody: the manager gets the same ask twice and has to
    # work out whether it is two orders or one impatient seller.
    existing = (
        ProductionRequest.objects.open()
        .filter(product=product, requested_by=requested_by, assigned_to=assigned_to)
        .first()
    )
    if existing is not None:
        raise ValidationError(
            f"You already have an open request with "
            f"{assigned_to.display_name} for {product.name} "
            f"({existing.quantity} units). Cancel it first if it has changed."
        )

    option, label = resolve_option(
        REQUEST_REASON_GROUP,
        label=reason_name,
        option_id=reason_id,
        user=requested_by,
    )

    request = ProductionRequest.objects.create(
        product=product,
        quantity=quantity,
        requested_by=requested_by,
        assigned_to=assigned_to,
        reason=option,
        reason_name=label,
        note=(note or "").strip()[:255],
        note_tag=note_tag,
        needed_by=needed_by,
        stock_at_request=product.stock_quantity,
    )
    if option is not None:
        option.touch_use()

    _notify_request(
        request,
        recipients=[assigned_to],
        title="Production needed",
        body=(
            f"{requested_by.display_name} needs {quantity} x {product.name}. "
            f"Only {product.stock_quantity} left"
            + (f". {label}" if label else "")
        ),
    )

    logger.info(
        "Production request #%s: %s asked %s for %d x %s",
        request.pk,
        getattr(requested_by, "username", "?"),
        getattr(assigned_to, "username", "?"),
        quantity,
        product.sku,
    )
    return request


@transaction.atomic
def respond_to_request(request, *, user, accept: bool, note: str = ""):
    """Accept or decline. Only the person asked - or an admin - may answer."""
    locked = ProductionRequest.objects.select_for_update().get(pk=request.pk)

    if locked.assigned_to_id != user.pk and not user.is_admin:
        raise ValidationError("This request was not addressed to you.")
    if locked.status != RequestStatus.PENDING:
        raise ValidationError(
            f"This request has already been answered "
            f"({locked.get_status_display().lower()})."
        )

    locked.status = RequestStatus.ACCEPTED if accept else RequestStatus.DECLINED
    locked.responded_at = timezone.now()
    locked.response_note = (note or "").strip()[:255]
    locked.save(update_fields=["status", "responded_at", "response_note", "updated_at"])

    _notify_request(
        locked,
        recipients=[locked.requested_by],
        title="Request accepted" if accept else "Request declined",
        body=(
            f"{user.display_name} "
            + ("will produce " if accept else "declined ")
            + f"{locked.quantity} x {locked.product.name}."
            + (f" {locked.response_note}" if locked.response_note else "")
        ),
    )
    return locked


@transaction.atomic
def cancel_request(request, *, user, note: str = ""):
    """Withdraw a request. Only the person who raised it, or an admin."""
    locked = ProductionRequest.objects.select_for_update().get(pk=request.pk)

    if locked.requested_by_id != user.pk and not user.is_admin:
        raise ValidationError("Only the person who asked can cancel this.")
    if not locked.is_open:
        raise ValidationError("This request is already closed.")

    locked.status = RequestStatus.CANCELLED
    locked.responded_at = timezone.now()
    locked.response_note = (note or "").strip()[:255]
    locked.save(update_fields=["status", "responded_at", "response_note", "updated_at"])

    _notify_request(
        locked,
        recipients=[locked.assigned_to],
        title="Request cancelled",
        body=(
            f"{user.display_name} no longer needs "
            f"{locked.quantity} x {locked.product.name}."
        ),
    )
    return locked


def _close_requests(request_ids, *, run, user):
    """Mark the named requests produced, and tell whoever was waiting."""
    ids = [int(i) for i in request_ids if str(i).isdigit()]
    if not ids:
        return

    rows = ProductionRequest.objects.open().filter(
        pk__in=ids, product=run.product
    ).select_related("requested_by", "product")

    for row in rows:
        row.status = RequestStatus.FULFILLED
        row.fulfilled_run = run
        row.responded_at = timezone.now()
        row.save(update_fields=[
            "status", "fulfilled_run", "responded_at", "updated_at",
        ])
        _notify_request(
            row,
            recipients=[row.requested_by],
            title="Your stock is ready",
            body=(
                f"{run.quantity_produced} x {run.product.name} were produced "
                f"in {run.reference} and are on the shelf."
            ),
        )


def _notify_request(request, *, recipients, title, body):
    """
    Push, and never at the cost of the record.

    Wrapped because api.push is an optional dependency of this module - the
    yard must keep working if Firebase is misconfigured, and a request that
    rolled back because a notification failed would be a worse outcome than a
    request nobody was buzzed about.
    """
    try:
        from api import push

        push.notify_users(
            [r for r in recipients if r is not None],
            title=title,
            body=body,
            channel="stock",
            data={
                "screen": "ProductionRequests",
                "requestId": request.pk,
                "productId": request.product_id,
            },
        )
    except Exception:  # pragma: no cover - never break the write
        logger.exception("Could not notify about production request %s", request.pk)


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
