"""
Credit business logic - THE implementation guide in executable form.

Read this file top to bottom to understand the whole borrower lifecycle:

    open_debt()        a credit sale creates a debt
    record_repayment() a customer pays something back
    settle_debt()      a customer clears the whole balance at once
    reverse_repayment()an Admin corrects a mis-keyed payment
    write_off_debt()   an Admin declares the debt uncollectable
    cancel_debt()      the underlying sale was voided

Every one of these functions:
  * runs inside a single atomic DB transaction,
  * locks the DebtRecord row with SELECT ... FOR UPDATE,
  * writes ledger rows rather than mutating balances,
  * calls recalculate() to refresh the cached figures,
  * cascades upward to the CreditAccount and the Transaction.
"""
import logging
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import transaction as db_transaction
from django.utils import timezone

from core.scoping import can_touch, owned_by
from core.utils import ZERO, default_due_date, money

from .models import CreditAccount, DebtRecord, DebtStatus, Repayment, RepaymentProof

logger = logging.getLogger(__name__)


class CreditError(ValidationError):
    pass


# ---------------------------------------------------------------------------
# STEP 1 - a credit sale opens a debt
# ---------------------------------------------------------------------------
@db_transaction.atomic
def open_debt(*, transaction, user=None, due_date=None, notes: str = "") -> DebtRecord:
    """
    Called by sales.services.create_sale() whenever balance_due > 0.

    principal is the UNPAID portion, not the sale total.
    """
    if transaction.customer is None:
        raise CreditError("A debt cannot be opened without a customer.")
    if transaction.balance_due <= ZERO:
        raise CreditError("This sale has no outstanding balance.")
    if hasattr(transaction, "debt_record") and transaction.debt_record is not None:
        raise CreditError(f"A debt already exists for {transaction.reference}.")

    account, _ = CreditAccount.objects.get_or_create(customer=transaction.customer)

    if due_date is None:
        due_date = default_due_date(account.default_terms_days)

    debt = DebtRecord.objects.create(
        # Inherited from the sale, not from the customer: the manager who let
        # the goods leave the shop is the one who has to collect for them.
        owner_id=transaction.owner_id or getattr(owned_by(user), "pk", None),
        customer=transaction.customer,
        transaction=transaction,
        principal=money(transaction.balance_due),
        amount_repaid=ZERO,
        balance=money(transaction.balance_due),
        status=DebtStatus.OPEN,
        issued_date=timezone.localdate(),
        due_date=due_date,
        created_by=user,
        notes=notes,
    )

    account.recalculate()

    logger.info(
        "Debt %s opened for %s: principal=%s due=%s",
        debt.reference, transaction.customer.name, debt.principal, debt.due_date,
    )
    return debt


# ---------------------------------------------------------------------------
# STEP 2 - the customer pays something back
# ---------------------------------------------------------------------------
@db_transaction.atomic
def record_repayment(
    *,
    debt: DebtRecord,
    amount: Decimal,
    user,
    method: str = Repayment.Method.CASH,
    paid_at=None,
    external_reference: str = "",
    note: str = "",
    proof_files=None,
    allow_overpayment: bool = False,
) -> Repayment:
    """
    Post one installment against a debt.

    The debt row is locked FOR UPDATE first, so two clerks taking a payment
    from the same customer at the same moment cannot both read a stale
    balance and let the debt go negative.
    """
    amount = money(amount)
    if amount <= ZERO:
        raise CreditError("Repayment amount must be greater than zero.")

    # Ownership is checked at the service layer, not only in the view: this
    # writes to an append-only ledger, so a request that slips past a view
    # leaves a permanent mark that only an admin reversal can neutralise.
    if not can_touch(debt, user):
        raise CreditError("You do not have access to this debt.")

    # --- Lock the debt --------------------------------------------------
    locked = DebtRecord.objects.select_for_update().select_related("customer").get(pk=debt.pk)

    if locked.status == DebtStatus.CANCELLED:
        raise CreditError("This debt was cancelled - the sale was voided.")
    if locked.status == DebtStatus.SETTLED:
        raise CreditError(f"{locked.reference} is already fully settled.")
    if locked.status == DebtStatus.WRITTEN_OFF:
        raise CreditError(
            f"{locked.reference} has been written off. An administrator must "
            "restore it before payments can be accepted."
        )

    balance_before = locked.balance

    if amount > balance_before and not allow_overpayment:
        raise CreditError(
            f"Payment of {amount} exceeds the outstanding balance of "
            f"{balance_before}. Enter {balance_before} to settle in full."
        )

    # --- Write the ledger row -------------------------------------------
    repayment = Repayment.objects.create(
        debt=locked,
        amount=amount,
        method=method,
        paid_at=paid_at or timezone.now(),
        balance_before=balance_before,
        balance_after=money(max(balance_before - amount, ZERO)),
        external_reference=external_reference,
        note=note,
        received_by=user,
    )

    # --- Attach proof ----------------------------------------------------
    for f in (proof_files or []):
        RepaymentProof.objects.create(repayment=repayment, file=f, uploaded_by=user)

    # --- Cascade ---------------------------------------------------------
    locked.recalculate()
    _cascade_after_change(locked)

    logger.info(
        "Repayment %s: %s on %s (%s -> %s) by %s",
        repayment.reference, amount, locked.reference,
        balance_before, locked.balance, getattr(user, "username", "?"),
    )
    return repayment


def settle_debt(*, debt: DebtRecord, user, **kwargs) -> Repayment:
    """Convenience wrapper: pay off the entire remaining balance in one go."""
    locked = DebtRecord.objects.select_for_update().get(pk=debt.pk)
    if locked.balance <= ZERO:
        raise CreditError("There is nothing left to settle on this debt.")
    return record_repayment(debt=locked, amount=locked.balance, user=user, **kwargs)


@db_transaction.atomic
def bulk_settle_customer(*, customer, amount: Decimal, user, **kwargs) -> list[Repayment]:
    """
    Apply one lump sum across a customer's open debts, OLDEST FIRST.

    This is what happens when someone walks in and hands over cash without
    saying which invoice it is for. Oldest-first is the standard rule and
    keeps the aging report honest.
    """
    remaining = money(amount)
    if remaining <= ZERO:
        raise CreditError("Payment amount must be greater than zero.")
    if not can_touch(customer, user):
        raise CreditError("That customer is not in your customer list.")

    debts = list(
        DebtRecord.objects.select_for_update()
        .filter(customer=customer, status__in=[DebtStatus.OPEN, DebtStatus.PARTIAL])
        .order_by("issued_date", "id")
    )
    if not debts:
        raise CreditError(f"{customer.name} has no open debts.")

    total_owed = money(sum(d.balance for d in debts))
    if remaining > total_owed:
        raise CreditError(
            f"Payment of {remaining} exceeds total outstanding debt of {total_owed}."
        )

    receipts = []
    for debt in debts:
        if remaining <= ZERO:
            break
        applied = min(remaining, debt.balance)
        receipts.append(
            record_repayment(
                debt=debt, amount=applied, user=user,
                note=(kwargs.pop("note", "") or "")
                + f" [Auto-allocated from a {amount} lump-sum payment]",
                **kwargs,
            )
        )
        remaining = money(remaining - applied)

    logger.info(
        "Bulk settlement of %s for %s across %d debt(s).",
        amount, customer.name, len(receipts),
    )
    return receipts


# ---------------------------------------------------------------------------
# STEP 3 - corrections (Admin only)
# ---------------------------------------------------------------------------
@db_transaction.atomic
def reverse_repayment(*, repayment: Repayment, user, reason: str) -> Repayment:
    """
    Undo a mis-keyed payment WITHOUT deleting it.

    The original row stays, flagged is_reversed=True. recalculate() excludes
    reversed rows, so the balance corrects itself while the history of the
    mistake remains fully visible.
    """
    if not reason or not reason.strip():
        raise CreditError("A reason is required to reverse a payment.")
    if repayment.is_reversed:
        raise CreditError("This payment has already been reversed.")
    if not can_touch(repayment, user):
        raise CreditError("You do not have access to this payment.")

    locked_debt = DebtRecord.objects.select_for_update().get(pk=repayment.debt_id)

    repayment.is_reversed = True
    repayment.reversed_at = timezone.now()
    repayment.reversed_by = user
    repayment.reversal_reason = reason
    repayment.save(update_fields=[
        "is_reversed", "reversed_at", "reversed_by", "reversal_reason", "updated_at",
    ])

    locked_debt.recalculate()
    _cascade_after_change(locked_debt)

    logger.warning(
        "Repayment %s REVERSED by %s. Reason: %s",
        repayment.reference, getattr(user, "username", "?"), reason,
    )
    return repayment


@db_transaction.atomic
def write_off_debt(*, debt: DebtRecord, user, reason: str) -> DebtRecord:
    """Declare a debt uncollectable. Admin only. The balance stays visible."""
    if not reason or not reason.strip():
        raise CreditError("A reason is required to write off a debt.")
    if not can_touch(debt, user):
        raise CreditError("You do not have access to this debt.")

    locked = DebtRecord.objects.select_for_update().get(pk=debt.pk)
    if locked.status in {DebtStatus.SETTLED, DebtStatus.CANCELLED}:
        raise CreditError(f"{locked.reference} cannot be written off ({locked.get_status_display()}).")

    locked.status = DebtStatus.WRITTEN_OFF
    locked.written_off_by = user
    locked.write_off_reason = reason
    locked.settled_date = timezone.localdate()
    locked.save(update_fields=[
        "status", "written_off_by", "write_off_reason", "settled_date", "updated_at",
    ])

    _cascade_after_change(locked)

    logger.warning(
        "Debt %s WRITTEN OFF (%s) by %s. Reason: %s",
        locked.reference, locked.balance, getattr(user, "username", "?"), reason,
    )
    return locked


@db_transaction.atomic
def restore_debt(*, debt: DebtRecord, user, reason: str = "") -> DebtRecord:
    """Undo a write-off and return the debt to collection."""
    locked = DebtRecord.objects.select_for_update().get(pk=debt.pk)
    if locked.status != DebtStatus.WRITTEN_OFF:
        raise CreditError("Only a written-off debt can be restored.")

    locked.status = DebtStatus.OPEN
    locked.written_off_by = None
    locked.write_off_reason = ""
    locked.settled_date = None
    locked.save(update_fields=[
        "status", "written_off_by", "write_off_reason", "settled_date", "updated_at",
    ])
    locked.recalculate()
    _cascade_after_change(locked)
    logger.warning("Debt %s restored by %s. %s", locked.reference,
                   getattr(user, "username", "?"), reason)
    return locked


@db_transaction.atomic
def cancel_debt(debt: DebtRecord, *, user, reason: str) -> DebtRecord:
    """Called when the underlying sale is voided. Removes it from A/R."""
    locked = DebtRecord.objects.select_for_update().get(pk=debt.pk)

    if locked.amount_repaid > ZERO:
        logger.warning(
            "Cancelling debt %s which already has %s in repayments - refund may be due.",
            locked.reference, locked.amount_repaid,
        )

    locked.status = DebtStatus.CANCELLED
    locked.balance = ZERO
    locked.notes = (locked.notes + f"\nCANCELLED: {reason}").strip()
    locked.save(update_fields=["status", "balance", "notes", "updated_at"])

    CreditAccount.objects.get_or_create(customer=locked.customer)[0].recalculate()
    return locked


# ---------------------------------------------------------------------------
# Cascade + maintenance
# ---------------------------------------------------------------------------
def _cascade_after_change(debt: DebtRecord):
    """
    After any change to a debt, push the new reality upward:
      debt -> CreditAccount aggregates
      debt -> the originating Transaction's payment status
    """
    account, _ = CreditAccount.objects.get_or_create(customer=debt.customer)
    account.recalculate()

    if debt.transaction_id:
        from sales.services import sync_transaction_from_debt

        debt.refresh_from_db()
        sync_transaction_from_debt(debt.transaction)


def update_credit_limit(*, account: CreditAccount, new_limit: Decimal, user, reason: str = ""):
    """Admin only - changing a credit limit is a financial decision."""
    account.credit_limit = money(new_limit)
    account.notes = (account.notes + f"\nLimit set to {new_limit} by "
                     f"{getattr(user, 'username', '?')}. {reason}").strip()
    account.save(update_fields=["credit_limit", "notes", "updated_at"])
    return account


def set_block(*, account: CreditAccount, blocked: bool, user, reason: str = ""):
    account.is_blocked = blocked
    account.block_reason = reason if blocked else ""
    account.save(update_fields=["is_blocked", "block_reason", "updated_at"])
    logger.info(
        "Credit %s for %s by %s. %s",
        "BLOCKED" if blocked else "unblocked",
        account.customer.name, getattr(user, "username", "?"), reason,
    )
    return account


def aging_summary(queryset=None, user=None) -> dict:
    """
    Bucketed accounts-receivable aging - the standard A/R report.

    Pass `user` to restrict the report to that person's own debts. Passing
    neither a queryset nor a user reports on the whole business, which is
    what the reconciliation tasks want.
    """
    # NOTE: must test `is None`, not truthiness. An empty queryset is falsy,
    # so `queryset or DebtRecord.objects.all()` would silently show EVERY
    # debt in the system for a customer who happens to owe nothing.
    base = DebtRecord.objects.all() if queryset is None else queryset
    if user is not None:
        from core.scoping import scoped

        base = scoped(base, user)
    qs = base.open_debts()
    buckets = {"CURRENT": ZERO, "1-30": ZERO, "31-60": ZERO, "61-90": ZERO, "90+": ZERO}
    counts = {k: 0 for k in buckets}
    for debt in qs.only("due_date", "balance", "status"):
        bucket = debt.aging_bucket
        if bucket in buckets:
            buckets[bucket] = money(buckets[bucket] + debt.balance)
            counts[bucket] += 1
    return {
        "buckets": buckets,
        "counts": counts,
        "total": money(sum(buckets.values())),
        "total_count": sum(counts.values()),
    }


def reconcile_all_accounts() -> dict:
    """Nightly maintenance: replay every ledger and report any drift."""
    drift = {}
    for debt in DebtRecord.objects.exclude(status=DebtStatus.CANCELLED):
        cached = debt.balance
        debt.recalculate()
        if cached != debt.balance:
            drift[debt.reference] = {"cached": str(cached), "corrected": str(debt.balance)}
            logger.warning("Debt drift on %s: %s -> %s", debt.reference, cached, debt.balance)
    for account in CreditAccount.objects.all():
        account.recalculate()
    return drift
