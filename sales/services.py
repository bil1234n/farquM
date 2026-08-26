"""
Sale orchestration. Every sale is created here, inside one DB transaction,
so stock, money and debt can never end up half-written.
"""
import logging
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import transaction as db_transaction
from django.utils import timezone

from core.scoping import can_touch, owned_by, sees_everything
from core.utils import ZERO, money
from inventory.services import deduct_for_sale, reverse_sale

from .models import (
    Customer,
    PaymentMethod,
    PaymentStatus,
    Transaction,
    TransactionItem,
)

logger = logging.getLogger(__name__)


class SaleError(ValidationError):
    pass


@db_transaction.atomic
def create_sale(
    *,
    user,
    cart: list[dict],
    customer: Customer | None = None,
    amount_paid: Decimal = ZERO,
    discount_amount: Decimal = ZERO,
    tax_amount: Decimal = ZERO,
    payment_method: str = PaymentMethod.CASH,
    due_date=None,
    notes: str = "",
) -> Transaction:
    """
    Record a complete sale.

    cart is a list of dicts:
        [{"product": <Product>, "quantity": 3, "unit_price": Decimal("50.00"),
          "line_discount": Decimal("0.00")}, ...]

    Sequence (order matters):
      1. Validate the cart and the credit position.
      2. Create the Transaction header (gets its reference).
      3. Create each line item + deduct stock through the ledger.
      4. Recalculate totals from the line items.
      5. If a balance remains, open a DebtRecord in the credit module.
    """
    if not cart:
        raise SaleError("Cannot record a sale with no items.")

    # --- 0. Ownership -------------------------------------------------------
    # Enforced here rather than only in the view, because this is the single
    # doorway every client goes through. A crafted request that names another
    # manager's product ID must fail even if the view forgot to scope.
    _validate_ownership(user, cart, customer)

    amount_paid = money(amount_paid)
    discount_amount = money(discount_amount)
    tax_amount = money(tax_amount)

    if amount_paid < ZERO:
        raise SaleError("Amount paid cannot be negative.")

    # --- 1. Pre-flight validation ------------------------------------------
    provisional_total = ZERO
    for line in cart:
        product = line["product"]
        qty = int(line["quantity"])
        price = money(line.get("unit_price", product.selling_price))
        line_disc = money(line.get("line_discount", ZERO))

        if qty <= 0:
            raise SaleError(f"Quantity for '{product.name}' must be at least 1.")
        if price < ZERO:
            raise SaleError(f"Unit price for '{product.name}' cannot be negative.")
        if not product.allow_negative_stock and qty > product.stock_quantity:
            raise SaleError(
                f"Not enough stock for '{product.name}'. "
                f"Available: {product.stock_quantity}, requested: {qty}."
            )
        provisional_total += (price * qty) - line_disc

    provisional_total = money(provisional_total - discount_amount + tax_amount)
    credit_needed = money(max(provisional_total - amount_paid, ZERO))

    if credit_needed > ZERO:
        _validate_credit_eligibility(customer, credit_needed)

    # --- 2. Header ----------------------------------------------------------
    txn = Transaction.objects.create(
        owner=owned_by(user),
        customer=customer,
        discount_amount=discount_amount,
        tax_amount=tax_amount,
        amount_paid=amount_paid,
        payment_method=(
            PaymentMethod.CREDIT if (credit_needed > ZERO and amount_paid == ZERO)
            else payment_method
        ),
        due_date=due_date if credit_needed > ZERO else None,
        sold_by=user,
        notes=notes,
    )

    # --- 3. Lines + stock ---------------------------------------------------
    for line in cart:
        product = line["product"]
        qty = int(line["quantity"])
        price = money(line.get("unit_price", product.selling_price))

        TransactionItem.objects.create(
            transaction=txn,
            product=product,
            product_name=product.name,
            product_sku=product.sku,
            quantity=qty,
            unit_price=price,
            unit_cost=product.cost_price,          # <- the snapshot
            line_discount=money(line.get("line_discount", ZERO)),
        )
        deduct_for_sale(
            product, qty, user=user, reference=txn.reference, unit_price=price
        )

    # --- 4. Totals ----------------------------------------------------------
    txn.recalculate_totals()

    # --- 5. Debt ------------------------------------------------------------
    if txn.balance_due > ZERO:
        from credit.services import open_debt

        open_debt(transaction=txn, user=user, due_date=due_date)

    logger.info(
        "Sale %s recorded by %s: total=%s paid=%s balance=%s",
        txn.reference, getattr(user, "username", "?"),
        txn.total_amount, txn.amount_paid, txn.balance_due,
    )
    return txn


def _validate_ownership(user, cart, customer):
    """
    Every product and the customer must belong to the person making the sale.

    An administrator is exempt: they can see and act on everything, and
    blocking them would make it impossible to cover for an absent manager.
    """
    if sees_everything(user):
        return

    for line in cart:
        product = line["product"]
        if not can_touch(product, user):
            # Deliberately vague. Confirming "that product exists but is
            # someone else's" is itself a disclosure.
            raise SaleError(
                f"'{product.name}' is not in your product list and cannot be sold "
                f"by you."
            )

    if customer is not None and not can_touch(customer, user):
        raise SaleError("That customer is not in your customer list.")


def _validate_credit_eligibility(customer, credit_needed: Decimal):
    """A credit sale requires a named, approved, un-blocked customer."""
    if customer is None:
        raise SaleError(
            "A credit sale needs a registered customer. "
            "Select or create the customer first, or collect full payment."
        )
    if not customer.is_active:
        raise SaleError(f"Customer '{customer.name}' is inactive.")
    if not customer.is_credit_approved:
        raise SaleError(
            f"'{customer.name}' is not approved for credit. "
            "An administrator must approve them first."
        )

    account = getattr(customer, "credit_account", None)
    if account is None:
        raise SaleError("This customer has no credit account.")
    if account.is_blocked:
        raise SaleError(
            f"Credit is blocked for '{customer.name}'. Reason: "
            f"{account.block_reason or 'not specified'}."
        )
    if account.credit_limit > ZERO:
        projected = money(account.outstanding_balance + credit_needed)
        if projected > account.credit_limit:
            raise SaleError(
                f"Credit limit exceeded for '{customer.name}'. "
                f"Limit: {account.credit_limit}, current debt: "
                f"{account.outstanding_balance}, this sale adds: {credit_needed}."
            )


@db_transaction.atomic
def void_transaction(txn: Transaction, *, user, reason: str) -> Transaction:
    """
    Admin override. Reverses stock and cancels any linked debt.
    The Transaction row itself is never deleted.
    """
    if not can_touch(txn, user):
        raise SaleError("You do not have access to this transaction.")
    if txn.is_voided:
        raise SaleError("This transaction has already been voided.")
    if not reason or not reason.strip():
        raise SaleError("A reason is required when voiding a transaction.")

    locked = Transaction.objects.select_for_update().get(pk=txn.pk)

    # Put the goods back on the shelf.
    for item in locked.items.select_related("product"):
        reverse_sale(
            item.product, item.quantity, user=user,
            reference=locked.reference,
            reason=f"Void of {locked.reference}: {reason}",
        )

    # Cancel the debt, if any.
    debt = getattr(locked, "debt_record", None)
    if debt is not None:
        from credit.services import cancel_debt

        cancel_debt(debt, user=user, reason=f"Sale {locked.reference} voided: {reason}")

    locked.is_voided = True
    locked.voided_at = timezone.now()
    locked.voided_by = user
    locked.void_reason = reason
    locked.payment_status = PaymentStatus.REFUNDED
    locked.balance_due = ZERO
    locked.save(update_fields=[
        "is_voided", "voided_at", "voided_by", "void_reason",
        "payment_status", "balance_due", "updated_at",
    ])

    logger.warning(
        "Transaction %s VOIDED by %s. Reason: %s",
        locked.reference, getattr(user, "username", "?"), reason,
    )
    return locked


def sync_transaction_from_debt(txn: Transaction) -> Transaction:
    """
    Called by the credit module after a repayment. Pulls the transaction's
    amount_paid back in line with what has actually been collected.
    """
    debt = getattr(txn, "debt_record", None)
    if debt is None:
        return txn

    collected_after_sale = debt.amount_repaid
    paid_at_till = money(txn.total_amount - debt.principal)

    txn.amount_paid = money(paid_at_till + collected_after_sale)
    txn.balance_due = money(max(txn.total_amount - txn.amount_paid, ZERO))
    txn.payment_status = txn.derive_payment_status()
    txn.save(update_fields=["amount_paid", "balance_due", "payment_status", "updated_at"])
    return txn
