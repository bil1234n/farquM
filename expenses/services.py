"""
Recording, correcting and cancelling expenses. The API and the web forms both
call these, so the rules - a bank transfer names its bank, a salary names its
person - cannot differ between the phone and the browser.
"""
import datetime as dt
import logging
from decimal import Decimal, InvalidOperation

from django.core.exceptions import ValidationError
from django.db import transaction as db_transaction
from django.db.models import Count, Sum
from django.utils import timezone

from core.models import resolve_option
from core.options import channel_group_for_method
from core.scoping import can_touch, owned_by
from core.utils import ZERO, money

from .models import Employee, Expense, ExpenseMethod

logger = logging.getLogger(__name__)

#: What a staff payment is filed under when nobody chose a category.
STAFF_CATEGORY = "Salaries & wages"
#: And what kind of payment it is when nobody said.
DEFAULT_PAY_TYPE = "Salary"


class ExpenseError(ValidationError):
    pass


def month_start(value) -> dt.date | None:
    """The first of the month `value` falls in - how a pay period is stored."""
    if value is None or value == "":
        return None
    if isinstance(value, str):
        try:
            value = dt.date.fromisoformat(value[:10])
        except ValueError:
            raise ExpenseError("The pay period is not a date.")
    if isinstance(value, dt.datetime):
        value = value.date()
    return value.replace(day=1)


def _amount(value) -> Decimal:
    try:
        amount = money(Decimal(str(value)))
    except (InvalidOperation, TypeError, ValueError):
        raise ExpenseError("Enter the amount as a number.")
    if amount <= ZERO:
        raise ExpenseError("The amount must be more than zero.")
    return amount


def _apply(expense: Expense, *, user, data: dict) -> Expense:
    """Validate `data` and write it onto `expense` (not saved)."""
    expense.amount = _amount(data.get("amount"))

    spent_on = data.get("spent_on") or timezone.localdate()
    if isinstance(spent_on, str):
        try:
            spent_on = dt.date.fromisoformat(spent_on[:10])
        except ValueError:
            raise ExpenseError("The date is not a date.")
    if spent_on > timezone.localdate():
        raise ExpenseError("An expense cannot be dated in the future.")
    expense.spent_on = spent_on

    method = (data.get("payment_method") or ExpenseMethod.CASH).upper()
    if method not in ExpenseMethod.values:
        raise ExpenseError("Choose how it was paid: cash, bank, mobile money or cheque.")
    expense.payment_method = method

    # -- Who was paid --------------------------------------------------------
    employee = data.get("employee")
    if employee is not None and not isinstance(employee, Employee):
        employee = Employee.objects.filter(pk=employee).first()
        if employee is None:
            raise ExpenseError("That employee does not exist.")
    expense.employee = employee

    # -- What it was for ------------------------------------------------------
    category, category_name = resolve_option(
        "EXPENSE_CATEGORY",
        label=data.get("category_name", ""),
        option_id=data.get("category"),
        user=user,
    )
    if category is None and employee is not None:
        category, category_name = resolve_option(
            "EXPENSE_CATEGORY", label=STAFF_CATEGORY, user=user
        )
    if category is None:
        raise ExpenseError("Choose what the money was spent on.")
    expense.category, expense.category_name = category, category_name

    if employee is not None:
        pay_type, pay_type_name = resolve_option(
            "EMPLOYEE_PAY_TYPE",
            label=data.get("pay_type_name", ""),
            option_id=data.get("pay_type"),
            user=user,
        )
        if pay_type is None:
            pay_type, pay_type_name = resolve_option(
                "EMPLOYEE_PAY_TYPE", label=DEFAULT_PAY_TYPE, user=user
            )
        expense.pay_type, expense.pay_type_name = pay_type, pay_type_name
        expense.pay_period = month_start(data.get("pay_period")) or spent_on.replace(day=1)
        expense.payee = (data.get("payee") or "").strip()[:160] or employee.name
    else:
        expense.pay_type, expense.pay_type_name, expense.pay_period = None, "", None
        expense.payee = (data.get("payee") or "").strip()[:160]

    # -- Which bank or wallet -------------------------------------------------
    group = channel_group_for_method(method)
    if group:
        channel, channel_name = resolve_option(
            group,
            label=data.get("payment_channel_name", ""),
            option_id=data.get("payment_channel"),
            user=user,
        )
        if channel is None:
            raise ExpenseError(
                "Choose which bank or wallet the money was paid from."
            )
        expense.payment_channel, expense.payment_channel_name = channel, channel_name
        expense.payment_reference = (data.get("payment_reference") or "").strip()[:80]
    else:
        expense.payment_channel, expense.payment_channel_name = None, ""
        expense.payment_reference = ""

    expense.notes = (data.get("notes") or "").strip()
    if "note_tag" in data:
        expense.note_tag = data.get("note_tag")
    return expense


def _count_uses(expense: Expense):
    for option in (expense.category, expense.pay_type, expense.payment_channel):
        if option is not None:
            option.touch_use()


@db_transaction.atomic
def record_expense(*, user, data: dict, receipt=None) -> Expense:
    expense = _apply(Expense(), user=user, data=data)
    expense.owner = owned_by(user)
    expense.recorded_by = user
    if receipt is not None:
        expense.receipt = receipt
    expense.save()
    _count_uses(expense)
    logger.info(
        "Expense %s recorded by %s: %s %s",
        expense.reference, getattr(user, "username", "?"),
        expense.amount, expense.category_name,
    )
    return expense


#: What may differ from one line of a payment to the next. Everything else -
#: the date, how it was paid, the receipt, the note - is shared.
LINE_FIELDS = (
    "amount", "category", "category_name",
    "employee", "pay_type", "pay_type_name", "payee",
)

#: More lines than this in one payment is a runaway form, not a payment.
MAX_LINES = 50


def _blank_line(line) -> bool:
    """A row the "add another" button left behind, never filled in."""
    return not any(
        str(line.get(key) or "").strip()
        for key in ("amount", "category", "category_name", "employee")
    )


@db_transaction.atomic
def record_expense_lines(
    *, user, data: dict, lines, receipt=None, production_run=None
) -> list[Expense]:
    """
    One payment, several lines: three workers paid out of one envelope, fuel
    and a repair on one receipt, the labour and the power for one batch.

    Every line becomes an Expense row of its own - so "what did Kebede get"
    and "what went on fuel" keep working line by line - and the lines share
    the date, how the money was paid, the receipt and the note. They also
    share the first line's reference (group_reference), so they can be shown
    and found together.

    `data` holds the shared fields, with the same keys record_expense takes;
    each entry of `lines` holds what differs (LINE_FIELDS). Every line is
    checked before any is written, so a mistake in the third line leaves
    nothing half-recorded.
    """
    lines = [dict(line) for line in (lines or []) if not _blank_line(line)]
    if not lines:
        raise ExpenseError("Add at least one line with an amount.")
    if len(lines) > MAX_LINES:
        raise ExpenseError(f"One payment can have at most {MAX_LINES} lines.")

    built = []
    for number, line in enumerate(lines, start=1):
        merged = dict(data)
        merged.update({key: line[key] for key in LINE_FIELDS if key in line})
        if merged.get("employee") and "payee" not in line:
            # A shared "paid to" is a shop or a landlord; a staff line is paid
            # to its own person, and says so.
            merged["payee"] = ""
        try:
            built.append(_apply(Expense(), user=user, data=merged))
        except ExpenseError as exc:
            if len(lines) == 1:
                raise
            raise ExpenseError(
                "; ".join(f"Line {number}: {message}" for message in exc.messages)
            )

    owner = owned_by(user)
    first = None
    for expense in built:
        expense.owner = owner
        expense.recorded_by = user
        expense.production_run = production_run
        if first is None:
            if receipt is not None:
                expense.receipt = receipt
        elif first.receipt:
            # The same photo for every line, stored once: the later lines
            # point at the file the first one uploaded.
            expense.receipt = first.receipt.name
        expense.save()
        first = first or expense
        _count_uses(expense)

    if len(built) > 1:
        Expense.objects.filter(pk__in=[e.pk for e in built]).update(
            group_reference=first.reference
        )
        for expense in built:
            expense.group_reference = first.reference

    logger.info(
        "Expense %s recorded by %s: %d line(s), %s in all",
        first.reference, getattr(user, "username", "?"), len(built),
        sum((e.amount for e in built), ZERO),
    )
    return built


def _recost_batch(expense: Expense):
    """A batch's cost per unit follows its cost lines when one changes."""
    if expense.production_run_id:
        from production.services import recost_run

        recost_run(expense.production_run)


@db_transaction.atomic
def update_expense(expense: Expense, *, user, data: dict, receipt=None) -> Expense:
    if not can_touch(expense, user):
        raise ExpenseError("You do not have access to this expense.")
    locked = Expense.objects.select_for_update().get(pk=expense.pk)
    if locked.is_voided:
        raise ExpenseError("A cancelled expense cannot be changed.")
    _apply(locked, user=user, data=data)
    if receipt is not None:
        locked.receipt = receipt
    locked.save()
    _recost_batch(locked)
    return locked


@db_transaction.atomic
def void_expense(expense: Expense, *, user, reason: str) -> Expense:
    reason = (reason or "").strip()
    if not reason:
        raise ExpenseError("Give a reason for cancelling this expense.")
    if not can_touch(expense, user):
        raise ExpenseError("You do not have access to this expense.")
    locked = Expense.objects.select_for_update().get(pk=expense.pk)
    if locked.is_voided:
        raise ExpenseError("This expense has already been cancelled.")
    locked.is_voided = True
    locked.voided_at = timezone.now()
    locked.voided_by = user
    locked.void_reason = reason
    locked.save(update_fields=[
        "is_voided", "voided_at", "voided_by", "void_reason", "updated_at",
    ])
    logger.warning(
        "Expense %s CANCELLED by %s. Reason: %s",
        locked.reference, getattr(user, "username", "?"), reason,
    )
    _recost_batch(locked)
    return locked


def summarize(queryset) -> dict:
    """
    Totals for a set of (already scoped and dated) expenses.

    Voided rows are dropped here rather than trusted to the caller, because a
    cancelled expense counted in a monthly total is the mistake that makes an
    owner stop believing the screen.
    """
    live = queryset.filter(is_voided=False)
    head = live.aggregate(total=Sum("amount"), count=Count("id"))
    staff = live.filter(employee__isnull=False).aggregate(t=Sum("amount"))["t"]
    # Paid to make a batch, and so already inside that batch's cost per unit
    # - and, through the product's cost price, inside the cost of every unit
    # sold. Still money out, so it stays in the total; a profit figure takes
    # it off only once (see the callers of this).
    in_product_cost = live.filter(production_run__isnull=False).aggregate(
        t=Sum("amount")
    )["t"]
    by_category = [
        {
            "category": row["category_name"] or "-",
            "total": row["total"] or ZERO,
            "count": row["count"],
        }
        for row in live.values("category_name")
        .annotate(total=Sum("amount"), count=Count("id"))
        .order_by("-total", "category_name")
    ]
    return {
        "total": head["total"] or ZERO,
        "count": head["count"] or 0,
        "staff_total": staff or ZERO,
        "in_product_cost": in_product_cost or ZERO,
        "by_category": by_category,
    }


def running_costs(summary: dict):
    """
    What to take off gross profit: the total, less what is already inside the
    cost of the goods. A batch's labour is in its cost per unit, so the sales
    of those units already paid for it once - taking it off again would
    report a loss that never happened.
    """
    return summary["total"] - summary.get("in_product_cost", ZERO)
