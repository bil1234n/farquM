"""
Expenses and the payroll, in the browser.

    /expenses/                      one month of spending, by category
    /expenses/new/                  record one (?employee=<id> to pay somebody)
    /expenses/<id>/edit/            correct one
    /expenses/<id>/void/            cancel one, with a reason
    /expenses/export/               the month as CSV, for the accountant
    /expenses/employees/            the payroll
    /expenses/employees/<id>/       one person and everything they were paid

Every write goes through expenses.services - the same functions the phone's
API calls - so a rule enforced on one front door is enforced on both.

Expenses are a ledger and are scoped like sales (a manager sees their own and
their team's, the owner sees everything). The payroll is shared, like the
product list: two managers who both pay the same guard must both find him.
"""
import csv
import datetime as dt

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.db.models import Q, Sum
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.generic import DetailView, ListView

from accounts.models import AuditAction
from accounts.services import log_action
from core.mixins import (
    OwnerScopedMixin,
    PermissionRequiredMixin,
    get_owned_or_404,
    require,
)
from core.models import resolve_option
from core.scoping import scoped
from core.utils import ZERO

from .forms import EmployeeForm, ExpenseForm, VoidExpenseForm
from .models import Employee, Expense
from .services import (
    ExpenseError,
    record_expense,
    summarize,
    update_expense,
    void_expense,
)


# ---------------------------------------------------------------------------
# Months
# ---------------------------------------------------------------------------
def month_from(request):
    """The month being looked at: ?month=2026-09, or this month."""
    today = timezone.localdate()
    raw = (request.GET.get("month") or "").strip()
    try:
        year, month = (int(part) for part in raw.split("-")[:2])
        start = dt.date(year, month, 1)
    except (TypeError, ValueError):
        start = today.replace(day=1)
    end = (start.replace(day=28) + dt.timedelta(days=4)).replace(day=1) - dt.timedelta(days=1)
    return start, end


def shift_month(start, months):
    index = start.year * 12 + (start.month - 1) + months
    return dt.date(index // 12, index % 12 + 1, 1)


def _messages_of(exc):
    return getattr(exc, "messages", None) or [str(exc)]


# ---------------------------------------------------------------------------
# Expenses
# ---------------------------------------------------------------------------
class ExpenseListView(OwnerScopedMixin, PermissionRequiredMixin, ListView):
    """
    One month of spending: what it came to, where it went, and every line.

    Filters narrow the table AND the totals together - a total that does not
    match the rows under it is the quickest way to lose an owner's trust.
    """

    required_permission = "expense.view"
    model = Expense
    template_name = "expenses/expense_list.html"
    context_object_name = "expenses"
    paginate_by = 50

    def filtered(self):
        self.start, self.end = month_from(self.request)
        qs = (
            super()
            .get_queryset()
            .filter(spent_on__gte=self.start, spent_on__lte=self.end)
            .select_related("employee", "recorded_by", "voided_by", "note_tag")
        )
        params = self.request.GET
        self.q = (params.get("q") or "").strip()
        if self.q:
            qs = qs.filter(
                Q(reference__icontains=self.q) | Q(payee__icontains=self.q)
                | Q(category_name__icontains=self.q) | Q(notes__icontains=self.q)
                | Q(employee__name__icontains=self.q)
            )
        self.category = (params.get("category") or "").strip()
        if self.category:
            qs = qs.filter(category_name__iexact=self.category)
        self.staff_only = params.get("staff") == "1"
        if self.staff_only:
            qs = qs.filter(employee__isnull=False)
        return qs

    def get_queryset(self):
        qs = self.filtered()
        self.show_voided = self.request.GET.get("voided") == "1"
        if not self.show_voided:
            qs = qs.filter(is_voided=False)
        return qs.order_by("-spent_on", "-id")

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        user = self.request.user
        summary = summarize(self.filtered())
        top = summary["total"] or ZERO
        for row in summary["by_category"]:
            row["percent"] = round(row["total"] * 100 / top) if top else 0

        today = timezone.localdate()
        this_month = scoped(Expense.objects.all(), user).filter(
            spent_on__gte=self.start, spent_on__lte=self.end
        )
        ctx.update({
            "start": self.start,
            "end": self.end,
            "prev_month": shift_month(self.start, -1),
            "next_month": shift_month(self.start, 1),
            "is_current_month": self.start == today.replace(day=1),
            "summary": summary,
            "today_total": (
                this_month.filter(is_voided=False, spent_on=today)
                .aggregate(t=Sum("amount"))["t"] or ZERO
            ),
            "q": self.q,
            "category": self.category,
            "staff_only": self.staff_only,
            "show_voided": self.show_voided,
            "categories": (
                this_month.exclude(category_name="")
                .values_list("category_name", flat=True).distinct()
                .order_by("category_name")
            ),
            "can_record": user.has_access("expense.record"),
            "can_void": user.has_access("expense.void"),
            "can_pay_staff": user.has_access("employee.view"),
        })
        return ctx


def _expense_form_context(form, expense=None):
    return {
        "form": form,
        "expense": expense,
        # Salary per person, for "pay the usual" in the browser.
        "salaries": {
            str(e.pk): str(e.monthly_salary)
            for e in form.fields["employee"].queryset
        },
    }


def expense_create(request):
    blocked = require(
        request, "expense.record",
        message="You do not have permission to record expenses.",
    )
    if blocked:
        return blocked

    initial = {}
    employee_id = request.GET.get("employee")
    if request.method == "GET" and employee_id and employee_id.isdigit():
        person = Employee.objects.filter(pk=employee_id, is_active=True).first()
        if person is not None:
            initial.update({
                "employee": person.pk,
                "amount": person.monthly_salary or None,
                "payee": person.name,
                "pay_period": timezone.localdate().replace(day=1),
            })

    form = ExpenseForm(request.POST or None, request.FILES or None, initial=initial)
    if request.method == "POST" and form.is_valid():
        try:
            expense = record_expense(
                user=request.user,
                data=form.service_data(),
                receipt=form.cleaned_data.get("receipt"),
            )
        except (ExpenseError, ValidationError) as exc:
            for msg in _messages_of(exc):
                form.add_error(None, msg)
        else:
            log_action(
                AuditAction.CREATE, instance=expense,
                description=(
                    f"Recorded expense {expense.reference}: {expense.amount} "
                    f"for {expense.category_name}"
                    + (f" ({expense.employee.name})" if expense.employee_id else "")
                ),
            )
            messages.success(
                request,
                f"{expense.reference} recorded: {expense.amount} for "
                f"{expense.category_name}.",
            )
            if "another" in request.POST:
                return redirect("expenses:expense_create")
            return redirect(
                f"{_list_url()}?month={expense.spent_on:%Y-%m}"
            )
    return render(
        request, "expenses/expense_form.html", _expense_form_context(form)
    )


def _list_url():
    from django.urls import reverse

    return reverse("expenses:expense_list")


def expense_edit(request, pk):
    blocked = require(
        request, "expense.record",
        message="You do not have permission to change expenses.",
    )
    if blocked:
        return blocked
    expense = get_owned_or_404(
        Expense.objects.select_related("employee", "note_tag"), request.user, pk=pk
    )
    if expense.is_voided:
        messages.warning(request, "A cancelled expense cannot be changed.")
        return redirect(f"{_list_url()}?month={expense.spent_on:%Y-%m}&voided=1")

    form = ExpenseForm(
        request.POST or None,
        request.FILES or None,
        initial=ExpenseForm.initial_for(expense),
        instance=expense,
    )
    if request.method == "POST" and form.is_valid():
        try:
            expense = update_expense(
                expense,
                user=request.user,
                data=form.service_data(),
                receipt=form.cleaned_data.get("receipt"),
            )
        except (ExpenseError, ValidationError) as exc:
            for msg in _messages_of(exc):
                form.add_error(None, msg)
        else:
            log_action(
                AuditAction.UPDATE, instance=expense,
                description=f"Corrected expense {expense.reference}.",
            )
            messages.success(request, f"{expense.reference} updated.")
            return redirect(f"{_list_url()}?month={expense.spent_on:%Y-%m}")
    return render(
        request, "expenses/expense_form.html", _expense_form_context(form, expense)
    )


def expense_void(request, pk):
    blocked = require(
        request, "expense.void",
        message="You do not have permission to cancel expenses.",
    )
    if blocked:
        return blocked
    expense = get_owned_or_404(Expense, request.user, pk=pk)
    form = VoidExpenseForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        reason = form.cleaned_data["reason"]
        try:
            void_expense(expense, user=request.user, reason=reason)
        except (ExpenseError, ValidationError) as exc:
            for msg in _messages_of(exc):
                form.add_error(None, msg)
        else:
            log_action(
                AuditAction.VOID, instance=expense,
                description=f"Cancelled expense {expense.reference}: {reason}",
            )
            messages.success(request, f"{expense.reference} cancelled.")
            return redirect(f"{_list_url()}?month={expense.spent_on:%Y-%m}")
    return render(
        request, "expenses/expense_void.html", {"form": form, "expense": expense}
    )


def expense_export(request):
    """The month on screen, as CSV - same filters, same scope."""
    blocked = require(request, "expense.view")
    if blocked:
        return blocked
    view = ExpenseListView()
    view.setup(request)
    rows = view.get_queryset()

    response = HttpResponse(content_type="text/csv")
    response["Content-Disposition"] = (
        f'attachment; filename="expenses-{view.start:%Y-%m}.csv"'
    )
    writer = csv.writer(response)
    writer.writerow([
        "Date", "Reference", "Category", "Paid to", "Employee", "Pay type",
        "For month", "Amount", "Paid by", "Bank / wallet", "Reference no.",
        "Notes", "Mark", "Recorded by", "Cancelled", "Reason cancelled",
    ])
    for e in rows.select_related("recorded_by"):
        writer.writerow([
            e.spent_on.isoformat(), e.reference, e.category_name, e.payee,
            e.employee.name if e.employee_id else "", e.pay_type_name,
            e.pay_period.strftime("%Y-%m") if e.pay_period else "",
            e.amount, e.get_payment_method_display(), e.payment_channel_name,
            e.payment_reference, e.notes, e.note_tag.label if e.note_tag_id else "",
            e.recorded_by.display_name if e.recorded_by_id else "",
            "yes" if e.is_voided else "", e.void_reason,
        ])
    return response


# ---------------------------------------------------------------------------
# Employees
# ---------------------------------------------------------------------------
def _paid_by_person(user, start, end):
    """{employee id: total} for payments this reader may see, in a range."""
    rows = (
        scoped(Expense.objects.filter(is_voided=False, employee__isnull=False), user)
        .filter(spent_on__gte=start, spent_on__lte=end)
        .values("employee_id")
        .annotate(total=Sum("amount"))
    )
    return {row["employee_id"]: row["total"] or ZERO for row in rows}


class EmployeeListView(PermissionRequiredMixin, ListView):
    """
    The payroll, with this month's pay beside each name - so "has everybody
    had their salary?" is answered by looking, not by adding up.
    """

    required_permission = "employee.view"
    model = Employee
    template_name = "expenses/employee_list.html"
    context_object_name = "employees"

    def get_queryset(self):
        qs = Employee.objects.select_related("job", "note_tag")
        self.q = (self.request.GET.get("q") or "").strip()
        if self.q:
            qs = qs.filter(
                Q(name__icontains=self.q) | Q(phone__icontains=self.q)
                | Q(job_name__icontains=self.q)
            )
        self.show_all = self.request.GET.get("all") == "1"
        if not self.show_all:
            qs = qs.filter(is_active=True)
        return qs.order_by("-is_active", "name")

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        user = self.request.user
        today = timezone.localdate()
        start = today.replace(day=1)
        paid = _paid_by_person(user, start, today)

        rows = []
        payroll = paid_total = to_pay = ZERO
        for person in ctx["employees"]:
            got = paid.get(person.pk, ZERO)
            salary = person.monthly_salary or ZERO
            rows.append({
                "person": person,
                "paid": got,
                "percent": min(round(got * 100 / salary), 100) if salary else 0,
                "outstanding": max(salary - got, ZERO),
            })
            if person.is_active:
                payroll += salary
                paid_total += got
                to_pay += max(salary - got, ZERO)
        ctx.update({
            "rows": rows,
            "q": self.q,
            "show_all": self.show_all,
            "month_start": start,
            "payroll": payroll,
            "paid_total": paid_total,
            "to_pay": to_pay,
            "active_count": Employee.objects.filter(is_active=True).count(),
            "can_manage": user.has_access("employee.manage"),
            "can_pay": user.has_access("expense.record"),
        })
        return ctx


class EmployeeDetailView(PermissionRequiredMixin, DetailView):
    required_permission = "employee.view"
    model = Employee
    template_name = "expenses/employee_detail.html"
    context_object_name = "person"

    def get_queryset(self):
        return Employee.objects.select_related("job", "note_tag", "created_by")

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        user = self.request.user
        today = timezone.localdate()
        payments = scoped(
            self.object.payments.select_related("recorded_by", "note_tag", "voided_by"),
            user,
        ).order_by("-spent_on", "-id")
        live = payments.filter(is_voided=False)
        ctx.update({
            "payments": payments[:100],
            "paid_this_month": live.filter(
                spent_on__gte=today.replace(day=1), spent_on__lte=today
            ).aggregate(t=Sum("amount"))["t"] or ZERO,
            "paid_this_year": live.filter(
                spent_on__gte=today.replace(month=1, day=1), spent_on__lte=today
            ).aggregate(t=Sum("amount"))["t"] or ZERO,
            "paid_ever": live.aggregate(t=Sum("amount"))["t"] or ZERO,
            "last_payment": live.first(),
            "can_manage": user.has_access("employee.manage"),
            "can_pay": user.has_access("expense.record") and self.object.is_active,
        })
        return ctx


def _save_employee(request, form, *, creating):
    employee = form.save(commit=False)
    job, job_name = resolve_option(
        "EMPLOYEE_JOB",
        label=form.cleaned_data.get("job_name", ""),
        option_id=form.cleaned_data.get("job"),
        user=request.user,
    )
    employee.job, employee.job_name = job, job_name
    if creating:
        employee.created_by = request.user
    employee.updated_by = request.user
    employee.save()
    if job is not None:
        job.touch_use()
    return employee


def employee_create(request):
    blocked = require(
        request, "employee.manage",
        message="You do not have permission to add employees.",
    )
    if blocked:
        return blocked
    form = EmployeeForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        employee = _save_employee(request, form, creating=True)
        log_action(
            AuditAction.CREATE, instance=employee,
            description=f"Added employee {employee.name}.",
        )
        messages.success(request, f"{employee.name} added to the payroll.")
        return redirect("expenses:employee_detail", pk=employee.pk)
    return render(request, "expenses/employee_form.html", {"form": form})


def employee_edit(request, pk):
    blocked = require(
        request, "employee.manage",
        message="You do not have permission to change employees.",
    )
    if blocked:
        return blocked
    employee = get_object_or_404(Employee, pk=pk)
    form = EmployeeForm(request.POST or None, instance=employee)
    if request.method == "POST" and form.is_valid():
        employee = _save_employee(request, form, creating=False)
        log_action(
            AuditAction.UPDATE, instance=employee,
            description=f"Updated employee {employee.name}.",
        )
        messages.success(request, f"{employee.name} updated.")
        return redirect("expenses:employee_detail", pk=employee.pk)
    return render(
        request, "expenses/employee_form.html", {"form": form, "person": employee}
    )
