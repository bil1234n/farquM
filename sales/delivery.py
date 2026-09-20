"""
Handing sold goods over - all at once, or a part at a time.

THE RULE THIS IS BUILT ON
-------------------------
Stock leaves the shelf at the moment of sale (sales.services.create_sale),
exactly as it always has. That is what stops a seller offering blocks that a
customer has already paid for but not yet collected. What was missing was the
other half: whether those goods have physically left the yard. That is what
lives here.

    sold      TransactionItem.quantity           what the customer paid for
    taken     TransactionItem.quantity_delivered what has gone out of the gate
    waiting   the difference                     theirs, still in the yard

A hand-over (Delivery) takes some of what is waiting for one sale; a sale
collected in three trips has three. Nothing here moves stock: the goods were
already off the shelf. It moves them from "waiting" to "gone".

Every write runs in one transaction with the sale row locked, so two stock
keepers tapping "hand over" on the same sale at once cannot both succeed.
"""
import logging

from django.db import transaction as db_transaction
from django.db.models import F, Sum
from django.utils import timezone

from core.scoping import can_touch

from .models import (
    Delivery,
    DeliveryLine,
    DeliveryStatus,
    Transaction,
    TransactionItem,
)
from .services import SaleError

logger = logging.getLogger(__name__)


class DeliveryError(SaleError):
    pass


def derive_status(items) -> str:
    """Where a sale stands, from its lines."""
    sold = sum(item.quantity for item in items)
    taken = sum(item.quantity_delivered for item in items)
    if sold and taken >= sold:
        return DeliveryStatus.DELIVERED
    if taken > 0:
        return DeliveryStatus.PARTIAL
    return DeliveryStatus.PENDING


def refresh_status(txn: Transaction) -> str:
    """Bring the sale's stored status back in line with its lines."""
    status = derive_status(list(txn.items.all()))
    if txn.delivery_status != status:
        txn.delivery_status = status
        txn.save(update_fields=["delivery_status", "updated_at"])
    return status


def _item_id(value):
    if isinstance(value, TransactionItem):
        return value.pk
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


@db_transaction.atomic
def record_delivery(
    txn: Transaction,
    *,
    user,
    lines=None,
    everything: bool = False,
    received_by_name: str = "",
    received_by_phone: str = "",
    vehicle: str = "",
    notes: str = "",
    note_tag=None,
    delivered_at=None,
) -> Delivery:
    """
    Record goods leaving the yard against one sale.

    `lines` is a list of {"item": <TransactionItem or id>, "quantity": n}. A
    line at zero means "none of this one today" and is simply skipped, so a
    form can post every line of the sale. `everything=True` takes whatever is
    still waiting on every line - the common case, one tap.
    """
    if not can_touch(txn, user):
        raise DeliveryError("You do not have access to this sale.")

    locked = Transaction.objects.select_for_update().get(pk=txn.pk)
    if locked.is_voided:
        raise DeliveryError("This sale was voided, so there is nothing to hand over.")

    items = {
        item.pk: item
        for item in locked.items.select_for_update().order_by("id")
    }

    wanted: dict[int, int] = {}
    if everything:
        for item in items.values():
            if item.quantity_waiting > 0:
                wanted[item.pk] = item.quantity_waiting
    else:
        for entry in lines or []:
            item_id = _item_id(entry.get("item"))
            try:
                quantity = int(entry.get("quantity") or 0)
            except (TypeError, ValueError):
                raise DeliveryError("Quantities must be whole numbers.")
            if quantity == 0:
                continue
            if quantity < 0:
                raise DeliveryError("A quantity cannot be negative.")
            item = items.get(item_id)
            if item is None:
                raise DeliveryError("That line is not part of this sale.")
            if item.pk in wanted:
                raise DeliveryError(f"{item.product_name} is listed twice.")
            if quantity > item.quantity_waiting:
                raise DeliveryError(
                    f"Only {item.quantity_waiting} of {item.product_name} "
                    f"are still waiting for this customer."
                )
            wanted[item.pk] = quantity

    if not wanted:
        if all(item.quantity_waiting == 0 for item in items.values()):
            raise DeliveryError(
                "Everything on this sale has already been handed over."
            )
        raise DeliveryError("Enter how many of at least one item are going out.")

    delivery = Delivery.objects.create(
        transaction=locked,
        delivered_by=user,
        delivered_at=delivered_at or timezone.now(),
        received_by_name=" ".join((received_by_name or "").split())[:160],
        received_by_phone=(received_by_phone or "").strip()[:30],
        vehicle=(vehicle or "").strip()[:40],
        notes=(notes or "").strip(),
        note_tag=note_tag,
    )
    DeliveryLine.objects.bulk_create(
        DeliveryLine(delivery=delivery, item_id=item_id, quantity=quantity)
        for item_id, quantity in wanted.items()
    )
    for item_id, quantity in wanted.items():
        TransactionItem.objects.filter(pk=item_id).update(
            quantity_delivered=F("quantity_delivered") + quantity
        )

    status = refresh_status(locked)
    logger.info(
        "Delivery %s for %s by %s: %s units, sale now %s",
        delivery.reference, locked.reference, getattr(user, "username", "?"),
        sum(wanted.values()), status,
    )
    return delivery


@db_transaction.atomic
def void_delivery(delivery: Delivery, *, user, reason: str) -> Delivery:
    """
    Take back a hand-over: recorded by mistake, or the goods came back.

    The quantities return to "waiting" for the customer - they are theirs,
    paid for - and the Delivery row stays, marked void, with the reason.
    """
    reason = (reason or "").strip()
    if not reason:
        raise DeliveryError("Give a reason for cancelling this hand-over.")

    txn = Transaction.objects.select_for_update().get(pk=delivery.transaction_id)
    if not can_touch(txn, user):
        raise DeliveryError("You do not have access to this sale.")

    locked = Delivery.objects.select_for_update().get(pk=delivery.pk)
    if locked.is_voided:
        raise DeliveryError("This hand-over has already been cancelled.")

    for line in locked.lines.all():
        TransactionItem.objects.filter(pk=line.item_id).update(
            quantity_delivered=F("quantity_delivered") - line.quantity
        )

    locked.is_voided = True
    locked.voided_at = timezone.now()
    locked.voided_by = user
    locked.void_reason = reason
    locked.save(update_fields=[
        "is_voided", "voided_at", "voided_by", "void_reason", "updated_at",
    ])
    refresh_status(txn)
    logger.warning(
        "Delivery %s CANCELLED by %s. Reason: %s",
        locked.reference, getattr(user, "username", "?"), reason,
    )
    return locked


def waiting_by_product(product_ids=None) -> dict[int, int]:
    """
    Units sold but still in the yard, per product.

    Business-wide on purpose, like the stock figure beside it: it is a count
    of physical goods standing in one yard, whoever sold them. It says how
    many, never to whom or for how much.
    """
    qs = TransactionItem.objects.filter(
        transaction__is_voided=False,
    ).exclude(transaction__delivery_status=DeliveryStatus.DELIVERED)
    if product_ids is not None:
        qs = qs.filter(product_id__in=list(product_ids))
    rows = qs.values("product_id").annotate(
        sold=Sum("quantity"), taken=Sum("quantity_delivered")
    )
    return {
        row["product_id"]: max((row["sold"] or 0) - (row["taken"] or 0), 0)
        for row in rows
        if (row["sold"] or 0) > (row["taken"] or 0)
    }


def queue_summary(user) -> dict:
    """
    The stock keeper's day in numbers: what is waiting, what went out today,
    and the oldest sales still standing in the yard.

    Scoped like everything else a user reads, so a seller asking sees their
    own customers' goods and the stock keeper, who sees every sale, sees the
    whole yard.
    """
    from core.scoping import scoped

    today = timezone.localdate()
    waiting_sales = scoped(Transaction.objects.awaiting_collection(), user)

    totals = TransactionItem.objects.filter(transaction__in=waiting_sales).aggregate(
        sold=Sum("quantity"), taken=Sum("quantity_delivered")
    )
    waiting_units = max((totals["sold"] or 0) - (totals["taken"] or 0), 0)

    handed_today = scoped(
        Delivery.objects.filter(is_voided=False, delivered_at__date=today), user
    )
    today_units = (
        DeliveryLine.objects.filter(delivery__in=handed_today).aggregate(
            n=Sum("quantity")
        )["n"]
        or 0
    )

    oldest = []
    for sale in waiting_sales.select_related("customer").prefetch_related(
        "items"
    ).order_by("created_at")[:5]:
        oldest.append({
            "id": sale.pk,
            "reference": sale.reference,
            "customer": sale.customer_display,
            "created_at": sale.created_at,
            "status": sale.delivery_status,
            "waiting_units": sum(i.quantity_waiting for i in sale.items.all()),
        })

    return {
        "waiting_sales": waiting_sales.count(),
        "partial_sales": waiting_sales.filter(
            delivery_status=DeliveryStatus.PARTIAL
        ).count(),
        "waiting_units": waiting_units,
        "today_handovers": handed_today.count(),
        "today_units": today_units,
        "oldest": oldest,
    }
