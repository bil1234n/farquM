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
        "by_category": by_category,
    }
