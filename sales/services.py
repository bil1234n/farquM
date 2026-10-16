"""
Sale orchestration. Every sale is created here, inside one DB transaction,
so stock, money and debt can never end up half-written.
"""
import logging
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import transaction as db_transaction
from django.utils import timezone

from core.branching import branch_for, same_branch
from core.models import resolve_option
from core.options import channel_group_for_method
from core.scoping import can_touch, owned_by, sees_everything
from core.utils import ZERO, money
from inventory.services import deduct_for_sale, reverse_sale

from .models import (
    Customer,
    ExtraCharge,
    PaymentMethod,
    PaymentStatus,
    Transaction,
    TransactionItem,
)

logger = logging.getLogger(__name__)

#: Extra charges on one sale. More than this is a runaway form, not a sale.
MAX_EXTRA_CHARGES = 20

#: The expense category a pay-out for an extra charge is filed under. One
#: heading for all of them, so the month's money out shows at a glance what
#: went on the extras sold with goods - and those rows are exactly the ones a
#: profit already takes off through the sales (see Transaction.total_cost).
#: Created in the branch's list the first time it is needed.
PAID_OUT_CATEGORY = "Sale extras"


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
    extra_charges: list[dict] | None = None,
    payment_method: str = PaymentMethod.CASH,
    payment_channel_id=None,
    payment_channel_name: str = "",
    payment_reference: str = "",
    walk_in_name: str = "",
    walk_in_phone: str = "",
    due_date=None,
    notes: str = "",
    note_tag=None,
) -> Transaction:
    """
    Record a complete sale.

    cart is a list of dicts:
        [{"product": <Product>, "quantity": 3, "unit_price": Decimal("50.00"),
          "line_discount": Decimal("0.00")}, ...]

    extra_charges is what the customer pays on top of the goods - see
    ExtraCharge:
        [{"label": "Transport", "amount": "500.00", "on_debt": False,
          "cost_amount": "400.00"}, ...]
    They are part of what the customer owes, so they count towards the
    credit check before anything is written. cost_amount ("We paid out") is
    optional; when given it is also recorded as an expense.

    Sequence (order matters):
      1. Validate the cart, the extra charges and the credit position.
      2. Create the Transaction header (gets its reference) and its extra
         charges, with the money-out row for each pay-out.
      3. Create each line item + deduct stock through the ledger.
      4. Recalculate totals from the lines.
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

    # Extras on top of the goods - transport, a loading worker... A sale can
    # carry several. Each is [{"label", "amount", "on_debt", "cost_amount"}];
    # a line with neither an amount nor a pay-out is simply dropped, and one
    # with an amount must say what it is for. See _clean_extra_charges.
    extras = _clean_extra_charges(extra_charges, user=user)
    extra_total = money(sum((e["amount"] for e in extras), ZERO))
    extra_debt_total = money(
        sum((e["amount"] for e in extras if e["on_debt"]), ZERO)
    )

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

    # Discounts are checked here, not only in the form, because this is the
    # one doorway every client uses. A hand-crafted POST or an API call that
    # bypassed the form would otherwise give money away.
    any_line_discount = any(money(l.get("line_discount", ZERO)) > ZERO for l in cart)
    if (discount_amount > ZERO or any_line_discount) and not _may(user, "sale.discount"):
        raise SaleError(
            "You do not have permission to apply a discount. Record the sale "
            "at full price, or ask someone who can approve the discount."
        )

    # The extras are priced and checked BEFORE anything is written, because
    # they are part of what the customer owes: a 500 birr lorry can be what
    # pushes a sale past a credit limit, and finding that out after the stock
    # has moved is not finding it out at all.
    goods_total = money(provisional_total - discount_amount + tax_amount)
    provisional_total = money(goods_total + extra_total)

    # Extra charges the seller said go on the customer's debt cannot be
    # swallowed by the amount paid: whatever is collected now must leave them
    # owing in full. Said here, in the one doorway every client uses, so the
    # web page and the phone cannot disagree about it.
    max_collectable = money(provisional_total - extra_debt_total)
    if extra_debt_total > ZERO and amount_paid > max_collectable:
        raise SaleError(
            f"Extra charges worth {extra_debt_total} are set to go on debt, so "
            f"no more than {max_collectable} can be collected now. Lower the "
            "amount paid, or choose to collect those extra charges now."
        )

    credit_needed = money(max(provisional_total - amount_paid, ZERO))

    if credit_needed > ZERO:
        if not _may(user, "sale.credit"):
            raise SaleError(
                "You do not have permission to sell on credit. Collect the "
                f"full amount ({provisional_total}) to complete this sale."
            )
        _validate_credit_eligibility(customer, credit_needed)

    # --- 2. Header ----------------------------------------------------------
    method = (
        PaymentMethod.CREDIT
        if (credit_needed > ZERO and amount_paid == ZERO)
        else payment_method
    )

    # A bank transfer names a bank, a wallet payment names a wallet, cash
    # names nothing. Resolved against the effective method rather than the
    # one that was sent, so a sale that falls back to CREDIT does not keep a
    # bank nobody transferred anything through.
    channel, channel_name = None, ""
    group = channel_group_for_method(method)
    if group:
        channel, channel_name = resolve_option(
            group,
            label=payment_channel_name,
            option_id=payment_channel_id,
            user=user,
        )

    # A one-off buyer: somebody who paid and left, and is never coming back.
    #
    # WHY THIS IS NOT A Customer ROW
    # ------------------------------
    # The customer book is for people you will sell to again - it carries a
    # credit limit, a balance and an aging position. Filing every passer-by in
    # it turns a list somebody actually works from into a phone directory, and
    # the debtor report into a haystack. The name and number still reach the
    # receipt, because they go into the snapshot columns the sale already
    # keeps for exactly this.
    #
    # No customer means no credit (see _validate_credit_eligibility), so a
    # walk-in is paid in full or it is not a sale.
    if customer is None:
        walk_in_name = " ".join((walk_in_name or "").split())[:160]
        walk_in_phone = (walk_in_phone or "").strip()[:30]
    else:
        walk_in_name, walk_in_phone = "", ""

    txn = Transaction.objects.create(
        # The branch being worked in; outside a request, the branch of the
        # customer or goods - see core.branching.branch_for.
        branch_id=branch_for(customer, *(line["product"] for line in cart)),
        owner=owned_by(user),
        customer=customer,
        customer_name_snapshot=walk_in_name,
        customer_phone_snapshot=walk_in_phone,
        discount_amount=discount_amount,
        tax_amount=tax_amount,
        # The summary of the lines below, so a total or a one-line reading
        # never needs a join.
        extra_charge_amount=extra_total,
        extra_charge_label=", ".join(e["label"] for e in extras)[:120],
        extra_charge_on_debt=extra_debt_total > ZERO,
        amount_paid=amount_paid,
        payment_method=method,
        payment_channel=channel,
        payment_channel_name=channel_name,
        payment_reference=(payment_reference or "").strip()[:80],
        due_date=due_date if credit_needed > ZERO else None,
        sold_by=user,
        notes=notes,
        note_tag=note_tag,
    )
    if channel is not None:
        # Counted so the three banks this yard actually uses float to the top
        # of a list of thirty next time.
        channel.touch_use()

    for extra in extras:
        _record_extra_charge(txn, extra, user=user)
    extra_cost = money(sum((e["cost_amount"] for e in extras), ZERO))
    if extra_cost > ZERO:
        # The total of the pay-outs, on the row, so a profit never needs a
        # join to find it. Written before the totals are worked out below,
        # which do not touch it.
        txn.extra_charge_cost = extra_cost
        txn.save(update_fields=["extra_charge_cost", "updated_at"])

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


def _may(user, code: str) -> bool:
    """Permission check that tolerates a user object without the method."""
    checker = getattr(user, "has_access", None)
    return bool(checker and checker(code))


# ---------------------------------------------------------------------------
# Extra charges: what the customer pays on top of the goods
# ---------------------------------------------------------------------------
def _extra_money(raw, what: str) -> Decimal:
    if raw in (None, ""):
        return ZERO
    try:
        value = money(str(raw).replace(",", "").strip())
    except Exception:
        raise SaleError(f"{what} must be a number, for example 500.")
    if value < ZERO:
        raise SaleError(f"{what} cannot be negative.")
    if value > Decimal("99999999.99"):
        raise SaleError(f"{what} is too large.")
    return value


def _clean_extra_charges(extra_charges, *, user) -> list[dict]:
    """
    Check the extra charges before a single row is written.

    Returns [{"label", "amount", "on_debt", "cost_amount"}] with the money
    parsed, so the caller can price the sale from them and then write them
    without parsing twice. A row with neither an amount nor a pay-out is a
    row the "Add" button left behind, and is dropped.
    """
    rows = []
    for raw in extra_charges or []:
        amount = _extra_money(raw.get("amount"), "An extra charge")
        cost = _extra_money(raw.get("cost_amount"), "What was paid out")
        if amount == ZERO and cost == ZERO:
            continue
        rows.append((raw, amount, cost))
    if len(rows) > MAX_EXTRA_CHARGES:
        raise SaleError(
            f"One sale can carry at most {MAX_EXTRA_CHARGES} extra charges."
        )

    extras = []
    for raw, amount, cost in rows:
        label = " ".join((raw.get("label") or "").split())[:120]
        if not label:
            raise SaleError(
                "Say what each extra charge is for (transport, worker...)."
            )
        if amount == ZERO:
            # A pay-out with nothing charged for it is a plain expense: the
            # customer's bill is not where it belongs.
            raise SaleError(
                f"{label}: enter what the customer is charged. If the business "
                "pays for it alone, record it under Expenses instead."
            )
        # Paying money out of the business is its own permission, wherever it
        # is done from. Without it the charge is still recorded - the customer
        # is billed - but the pay-out has to be entered by somebody who may
        # record expenses.
        if cost > ZERO and not _may(user, "expense.record"):
            raise SaleError(
                "You do not have permission to record money paid out. Leave "
                "\"We paid out\" empty, or ask someone who can record expenses."
            )
        extras.append({
            "label": label,
            "amount": amount,
            "on_debt": bool(raw.get("on_debt")),
            "cost_amount": cost,
        })
    return extras


def _record_extra_charge(txn: Transaction, extra: dict, *, user) -> ExtraCharge:
    """
    One extra on a sale, and the money-out row for what it cost.

    The pay-out is an ordinary expense under PAID_OUT_CATEGORY, so it shows
    in the month's money out like anything else; the line itself says what it
    was for in the expense's note.
    """
    charge = ExtraCharge.objects.create(
        transaction=txn,
        label=extra["label"],
        amount=extra["amount"],
        on_debt=extra["on_debt"],
        cost_amount=extra["cost_amount"],
    )

    if extra["cost_amount"] > ZERO:
        from expenses.services import record_expense

        # Dated with the sale and paid in cash by default: at a yard the
        # lorry driver and the loaders are paid from the till the same day.
        # Anything else is an edit on the expense afterwards, which is a
        # two-field correction rather than a fifth box at the counter.
        expense = record_expense(
            user=user,
            data={
                "amount": extra["cost_amount"],
                "spent_on": timezone.localdate(),
                "category_name": PAID_OUT_CATEGORY,
                "notes": f"{charge.label} on sale {txn.reference}.",
            },
        )
        expense.extra_charge = charge
        # The sale's own branch - the same one inside a request, and the one
        # that matters outside it.
        expense.branch_id = txn.branch_id
        expense.save(update_fields=["extra_charge", "branch", "updated_at"])
    return charge


def _validate_ownership(user, cart, customer):
    """
    Every product and the customer must belong to the person making the sale.

    An administrator is exempt: they can see and act on everything, and
    blocking them would make it impossible to cover for an absent manager.
    """
    # One branch's sale is made of that branch's goods and customers, for
    # whoever is selling - the owner included.
    for record in [*(line["product"] for line in cart), customer]:
        if record is not None and not same_branch(record):
            raise SaleError("That is not in this branch's books.")

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

    # Whether explicit approval is required is a business decision an
    # administrator makes once, in Settings. Blocked accounts and credit
    # limits below are NOT optional - those are per-customer judgements
    # somebody already made about this person, and a global switch has no
    # business overriding them.
    if not customer.is_credit_approved and _credit_approval_required():
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


def _credit_approval_required() -> bool:
    try:
        from core.models import SystemSetting

        return bool(SystemSetting.load().require_credit_approval)
    except Exception:
        # Fail closed: if the setting cannot be read, keep the stricter rule.
        return True


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

    # Goods that have physically left the yard are not coming back to the
    # shelf because a sale was voided on paper. Putting them back would give
    # the shelf stock that is sitting in a customer's lorry. The hand-overs
    # are cancelled first - which says the goods came back - and then the
    # sale can be voided and its stock returned honestly.
    if locked.deliveries.filter(is_voided=False).exists():
        raise SaleError(
            "Goods from this sale have already been handed over. Cancel "
            "those hand-overs first (the goods have come back), then void "
            "the sale."
        )

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

    # A voided sale took no lorry and hired no loaders, so the money said to
    # have gone out for them did not. Left standing, those rows would keep
    # pulling the month's profit down for a sale that no longer exists.
    _void_extra_charge_expenses(locked, user=user, reason=reason)

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


def _void_extra_charge_expenses(txn: Transaction, *, user, reason: str):
    """
    Cancel the money-out rows recorded for this sale's extra charges.

    Voided rather than deleted, like every other cancelled expense: the row
    stays on the record with who cancelled it and why.
    """
    from expenses.models import Expense

    rows = Expense.objects.filter(
        extra_charge__transaction=txn, is_voided=False
    )
    for expense in rows:
        expense.is_voided = True
        expense.voided_at = timezone.now()
        expense.voided_by = user
        expense.void_reason = f"Sale {txn.reference} voided: {reason}"
        expense.save(update_fields=[
            "is_voided", "voided_at", "voided_by", "void_reason", "updated_at",
        ])
        logger.warning(
            "Expense %s cancelled with sale %s", expense.reference, txn.reference
        )


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
