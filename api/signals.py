"""
What triggers a notification.

Deliberately narrow. A phone that buzzes for everything gets silenced, and
then it buzzes for nothing. Only events a person must act on are here.
"""
import logging

from django.db.models.signals import post_save
from django.dispatch import receiver

from core.utils import money

from . import push

logger = logging.getLogger(__name__)


@receiver(post_save, sender="inventory.StockMovement", dispatch_uid="api_stock_alert")
def stock_alert(sender, instance, created, **kwargs):
    """Alert when a movement takes a product to or below its threshold."""
    if not created or instance.quantity_delta >= 0:
        return

    product = instance.product
    after = instance.quantity_after

    if after > product.low_stock_threshold:
        return

    # Only fire on the crossing, not on every subsequent sale.
    if instance.quantity_before <= product.low_stock_threshold:
        return

    if after <= 0:
        title = "Out of stock"
        body = f"{product.name} has run out. It cannot be sold until restocked."
    else:
        title = "Low stock"
        body = (
            f"{product.name} is down to {after} "
            f"{product.get_unit_display().lower()}(s), at or below the alert level of "
            f"{product.low_stock_threshold}."
        )

    # Goes to the product's owner and to admins - NOT to every member of
    # staff. Buzzing a manager about stock they cannot see, in a shop they do
    # not run, is both a data leak and the fastest way to get notifications
    # turned off for good.
    push.notify_owner_and_admins(
        product.owner,
        title=title, body=body, channel="stock",
        data={"screen": "ProductDetail", "productId": product.pk},
    )


@receiver(post_save, sender="credit.DebtRecord", dispatch_uid="api_debt_opened")
def debt_opened(sender, instance, created, **kwargs):
    """Tell admins when a large amount goes out on credit."""
    if not created:
        return

    from django.conf import settings

    threshold = getattr(settings, "LARGE_CREDIT_ALERT", 0)
    if threshold <= 0 or instance.principal < threshold:
        return

    push.notify_owner_and_admins(
        instance.owner,
        title="Large credit sale",
        body=(
            f"{instance.customer.name} took {money(instance.principal)} on credit. "
            f"Due {instance.due_date:%d %b %Y}. Total owed: "
            f"{money(instance.customer.outstanding_balance)}."
        ),
        channel="credit",
        data={"screen": "DebtDetail", "debtId": instance.pk},
    )


@receiver(post_save, sender="credit.Repayment", dispatch_uid="api_repayment")
def repayment_received(sender, instance, created, **kwargs):
    if not created:
        return

    debt = instance.debt
    settled = instance.balance_after <= 0

    push.notify_owner_and_admins(
        debt.owner,
        title="Payment settled a debt" if settled else "Payment received",
        body=(
            f"{money(instance.amount)} from {debt.customer.name} "
            + ("cleared " if settled else "against ")
            + f"{debt.reference}."
            + ("" if settled else f" {money(instance.balance_after)} still outstanding.")
        ),
        channel="credit",
        data={"screen": "DebtDetail", "debtId": debt.pk},
    )


@receiver(post_save, sender="sales.Transaction", dispatch_uid="api_txn_voided")
def transaction_voided(sender, instance, created, **kwargs):
    """A void reverses stock and cancels debt - admins should know immediately."""
    if created or not instance.is_voided:
        return

    push.notify_owner_and_admins(
        instance.owner,
        title="Sale voided",
        body=(
            f"{instance.reference} ({money(instance.total_amount)}) was voided by "
            f"{instance.voided_by.display_name if instance.voided_by else 'someone'}. "
            f"Reason: {instance.void_reason[:120]}"
        ),
        channel="sales",
        data={"screen": "TransactionDetail", "transactionId": instance.pk},
    )
