from django.contrib import messages
from django.core.exceptions import ValidationError
from django.db.models import Count, DecimalField, Q, Sum
from django.db.models.functions import Coalesce
from django.shortcuts import redirect, render
from django.utils import timezone
from django.views.generic import DetailView, ListView, TemplateView, UpdateView

from accounts.models import AuditAction
from accounts.services import log_action
from core.mixins import (
    AdminRequiredMixin,
    OwnerScopedMixin,
    StaffRequiredMixin,
    get_owned_or_404,
)
from core.scoping import scoped
from core.utils import ZERO, money
from sales.models import Customer

from .forms import (
    BulkRepaymentForm,
    CreditAccountForm,
    DebtAdjustForm,
    DebtFilterForm,
    RepaymentForm,
    ReverseRepaymentForm,
    WriteOffForm,
)
from .models import CreditAccount, DebtRecord, DebtStatus, Repayment
from .services import (
    CreditError,
    aging_summary,
    bulk_settle_customer,
    record_repayment,
    reverse_repayment,
    set_block,
    write_off_debt,
)

DEC = DecimalField(max_digits=16, decimal_places=2)


def _sum(qs, field):
    return money(qs.aggregate(t=Coalesce(Sum(field, output_field=DEC), ZERO, output_field=DEC))["t"])


# ---------------------------------------------------------------------------
# Borrower dashboard
# ---------------------------------------------------------------------------
class CreditDashboardView(StaffRequiredMixin, TemplateView):
    """The 'Pay Later' control room: who owes what, and how late they are."""

    template_name = "credit/dashboard.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        today = timezone.localdate()
        user = self.request.user

        debts = scoped(DebtRecord.objects.all(), user)
        accounts = scoped(CreditAccount.objects.all(), user)
        payments = scoped(Repayment.objects.filter(is_reversed=False), user)

        open_debts = debts.open_debts()
        overdue = debts.overdue()

        ctx.update(
            {
                "total_outstanding": _sum(open_debts, "balance"),
                "total_overdue": _sum(overdue, "balance"),
                "open_count": open_debts.count(),
                "overdue_count": overdue.count(),
                "borrower_count": accounts.in_debt().count(),
                "aging": aging_summary(debts),
                "due_soon": (
                    debts.due_within(7)
                    .select_related("customer")
                    .order_by("due_date")[:10]
                ),
                "worst_overdue": (
                    overdue.select_related("customer").order_by("due_date")[:10]
                ),
                "top_borrowers": (
                    accounts.in_debt()
                    .select_related("customer")
                    .order_by("-outstanding_balance")[:10]
                ),
                "recent_payments": (
                    payments.select_related("debt", "debt__customer", "received_by")
                    .order_by("-paid_at")[:10]
                ),
                "collected_this_month": _sum(
                    payments.filter(paid_at__date__gte=today.replace(day=1)),
                    "amount",
                ),
                "over_limit": (
                    accounts.over_limit().select_related("customer")[:5]
                ),
                "today": today,
            }
        )
        return ctx


class BorrowerListView(OwnerScopedMixin, StaffRequiredMixin, ListView):
    """Every customer with a credit account, ranked by what they owe."""

    model = CreditAccount
    template_name = "credit/borrower_list.html"
    context_object_name = "accounts"
    paginate_by = 25

    def get_queryset(self):
        qs = super().get_queryset().select_related("customer").annotate(
            open_debt_count=Count(
                "customer__debts",
                filter=Q(customer__debts__status__in=[DebtStatus.OPEN, DebtStatus.PARTIAL]),
            )
        )
        q = self.request.GET.get("q", "").strip()
        flt = self.request.GET.get("filter", "").strip()
        if q:
            qs = qs.filter(
                Q(customer__name__icontains=q) | Q(customer__phone__icontains=q)
            )
        if flt == "debtors":
            qs = qs.filter(outstanding_balance__gt=0)
        elif flt == "overdue":
            overdue_ids = (
                scoped(DebtRecord.objects.all(), self.request.user)
                .overdue()
                .values_list("customer_id", flat=True)
            )
            qs = qs.filter(customer_id__in=overdue_ids)
        elif flt == "blocked":
            qs = qs.filter(is_blocked=True)
        elif flt == "clear":
            qs = qs.filter(outstanding_balance__lte=0)
        return qs.order_by("-outstanding_balance", "customer__name")

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["q"] = self.request.GET.get("q", "")
        ctx["selected_filter"] = self.request.GET.get("filter", "")
        ctx["total_outstanding"] = _sum(
            scoped(CreditAccount.objects.in_debt(), self.request.user),
            "outstanding_balance",
        )
        return ctx


class BorrowerDetailView(OwnerScopedMixin, StaffRequiredMixin, DetailView):
    """
    Full borrower profile: every debt, every installment, running balance.
    This is the screen you open when a customer disputes what they owe.
    """

    model = Customer
    template_name = "credit/borrower_detail.html"
    context_object_name = "customer"

    def get_queryset(self):
        return super().get_queryset().select_related("credit_account")

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        account, _ = CreditAccount.objects.get_or_create(customer=self.object)

        debts = (
            self.object.debts.select_related("transaction")
            .prefetch_related("repayments")
            .order_by("-created_at")
        )
        repayments = (
            Repayment.objects.filter(debt__customer=self.object)
            .select_related("debt", "received_by")
            .prefetch_related("proofs")
            .order_by("-paid_at")
        )

        ctx.update(
            {
                "account": account,
                "debts": debts,
                "open_debts": debts.filter(status__in=[DebtStatus.OPEN, DebtStatus.PARTIAL]),
                "repayments": repayments[:50],
                "repayment_count": repayments.count(),
                "bulk_form": BulkRepaymentForm(account=account),
                "aging": aging_summary(self.object.debts.all()),
                "total_borrowed": _sum(
                    debts.exclude(status=DebtStatus.CANCELLED), "principal"
                ),
                "total_repaid": _sum(
                    debts.exclude(status=DebtStatus.CANCELLED), "amount_repaid"
                ),
            }
        )
        return ctx


# ---------------------------------------------------------------------------
# Debts
# ---------------------------------------------------------------------------
class DebtListView(OwnerScopedMixin, StaffRequiredMixin, ListView):
    model = DebtRecord
    template_name = "credit/debt_list.html"
    context_object_name = "debts"
    paginate_by = 30

    def get_queryset(self):
        qs = super().get_queryset().select_related("customer", "transaction", "owner")
        form = DebtFilterForm(self.request.GET or None)
        if form.is_valid():
            q = form.cleaned_data.get("q")
            status = form.cleaned_data.get("status")
            bucket = form.cleaned_data.get("bucket")
            if q:
                qs = qs.filter(
                    Q(reference__icontains=q)
                    | Q(customer__name__icontains=q)
                    | Q(customer__phone__icontains=q)
                    | Q(transaction__reference__icontains=q)
                )
            if status == "OVERDUE":
                qs = qs.overdue()
            elif status:
                qs = qs.filter(status=status)
            if bucket:
                qs = self._filter_bucket(qs, bucket)
        self.filter_form = form
        return qs.order_by("status", "due_date")

    @staticmethod
    def _filter_bucket(qs, bucket):
        today = timezone.localdate()
        import datetime as dt

        qs = qs.open_debts()
        ranges = {
            "CURRENT": (today, None),
            "1-30": (today - dt.timedelta(days=30), today - dt.timedelta(days=1)),
            "31-60": (today - dt.timedelta(days=60), today - dt.timedelta(days=31)),
            "61-90": (today - dt.timedelta(days=90), today - dt.timedelta(days=61)),
            "90+": (None, today - dt.timedelta(days=91)),
        }
        low, high = ranges.get(bucket, (None, None))
        if bucket == "CURRENT":
            return qs.filter(due_date__gte=today)
        if low is not None:
            qs = qs.filter(due_date__gte=low)
        if high is not None:
            qs = qs.filter(due_date__lte=high)
        return qs

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        page_qs = self.get_queryset()
        ctx["filter_form"] = self.filter_form
        ctx["result_count"] = page_qs.count()
        ctx["sum_balance"] = _sum(page_qs, "balance")
        ctx["sum_principal"] = _sum(page_qs, "principal")
        return ctx


class DebtDetailView(OwnerScopedMixin, StaffRequiredMixin, DetailView):
    model = DebtRecord
    template_name = "credit/debt_detail.html"
    context_object_name = "debt"

    def get_queryset(self):
        return (
            super()
            .get_queryset()
            .select_related("customer", "transaction", "created_by", "written_off_by")
            .prefetch_related("repayments__proofs", "repayments__received_by")
        )

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["repayment_form"] = RepaymentForm(debt=self.object, user=self.request.user)
        ctx["repayments"] = self.object.repayments.select_related(
            "received_by", "reversed_by"
        ).prefetch_related("proofs").order_by("-paid_at")
        return ctx


class DebtAdjustView(OwnerScopedMixin, StaffRequiredMixin, UpdateView):
    """Reschedule a due date / add notes. No financial fields."""

    model = DebtRecord
    form_class = DebtAdjustForm
    template_name = "credit/debt_adjust.html"

    def form_valid(self, form):
        before_due = DebtRecord.objects.get(pk=self.object.pk).due_date
        response = super().form_valid(form)
        log_action(
            AuditAction.UPDATE, instance=self.object,
            description=(
                f"Rescheduled {self.object.reference}: due date "
                f"{before_due} -> {self.object.due_date}."
            ),
        )
        messages.success(self.request, "Debt updated.")
        return response


# ---------------------------------------------------------------------------
# Settlement - the core operational action
# ---------------------------------------------------------------------------
def repayment_create(request, pk):
    """Record a partial or full repayment against a single debt."""
    if not request.user.is_authenticated:
        return redirect("accounts:login")

    debt = get_owned_or_404(
        DebtRecord.objects.select_related("customer", "transaction"), request.user, pk=pk
    )
    form = RepaymentForm(
        request.POST or None, request.FILES or None, debt=debt, user=request.user
    )

    if request.method == "POST" and form.is_valid():
        try:
            repayment = record_repayment(
                debt=debt,
                amount=form.cleaned_data["amount"],
                user=request.user,
                method=form.cleaned_data["method"],
                paid_at=form.cleaned_data.get("paid_at"),
                external_reference=form.cleaned_data.get("external_reference", ""),
                note=form.cleaned_data.get("note", ""),
                proof_files=request.FILES.getlist("proof"),
            )
        except (CreditError, ValidationError) as exc:
            for msg in getattr(exc, "messages", [str(exc)]):
                messages.error(request, msg)
        else:
            debt.refresh_from_db()
            log_action(
                AuditAction.PAYMENT, instance=repayment,
                description=(
                    f"Received {repayment.amount} from {debt.customer.name} against "
                    f"{debt.reference}. Balance {repayment.balance_before} -> "
                    f"{repayment.balance_after}."
                ),
            )
            if debt.is_settled:
                messages.success(
                    request,
                    f"Payment recorded. {debt.reference} is now fully settled.",
                )
            else:
                messages.success(
                    request,
                    f"Payment of {repayment.amount} recorded. "
                    f"Remaining balance: {debt.balance}.",
                )
            return redirect("credit:debt_detail", pk=debt.pk)

    return render(
        request, "credit/repayment_form.html", {"form": form, "debt": debt}
    )


def bulk_repayment(request, pk):
    """
    Customer hands over a lump sum without naming an invoice.
    Applied oldest debt first.
    """
    if not request.user.is_authenticated:
        return redirect("accounts:login")

    customer = get_owned_or_404(
        Customer.objects.select_related("credit_account"), request.user, pk=pk
    )
    account, _ = CreditAccount.objects.get_or_create(customer=customer)
    form = BulkRepaymentForm(
        request.POST or None, request.FILES or None, account=account
    )

    if request.method == "POST" and form.is_valid():
        try:
            receipts = bulk_settle_customer(
                customer=customer,
                amount=form.cleaned_data["amount"],
                user=request.user,
                method=form.cleaned_data["method"],
                external_reference=form.cleaned_data.get("external_reference", ""),
                proof_files=request.FILES.getlist("proof"),
            )
        except (CreditError, ValidationError) as exc:
            for msg in getattr(exc, "messages", [str(exc)]):
                messages.error(request, msg)
        else:
            account.refresh_from_db()
            log_action(
                AuditAction.PAYMENT, instance=customer,
                description=(
                    f"Lump-sum payment of {form.cleaned_data['amount']} from "
                    f"{customer.name} allocated across {len(receipts)} debt(s), "
                    f"oldest first. Remaining debt: {account.outstanding_balance}."
                ),
            )
            messages.success(
                request,
                f"Payment applied across {len(receipts)} debt(s). "
                f"Remaining balance: {account.outstanding_balance}.",
            )
            return redirect("credit:borrower_detail", pk=customer.pk)

    return render(
        request,
        "credit/bulk_repayment.html",
        {"form": form, "customer": customer, "account": account},
    )


# ---------------------------------------------------------------------------
# Admin-only corrections
# ---------------------------------------------------------------------------
def repayment_reverse(request, pk):
    if not request.user.can_delete_records:
        messages.error(request, "Only an administrator may reverse a payment.")
        return redirect("core:forbidden")

    repayment = get_owned_or_404(
        Repayment.objects.select_related("debt", "debt__customer"), request.user, pk=pk
    )
    if repayment.is_reversed:
        messages.info(request, "This payment has already been reversed.")
        return redirect("credit:debt_detail", pk=repayment.debt_id)

    form = ReverseRepaymentForm(request.POST or None)

    if request.method == "POST" and form.is_valid():
        try:
            reverse_repayment(
                repayment=repayment, user=request.user,
                reason=form.cleaned_data["reason"],
            )
        except (CreditError, ValidationError) as exc:
            for msg in getattr(exc, "messages", [str(exc)]):
                messages.error(request, msg)
        else:
            log_action(
                AuditAction.OVERRIDE, instance=repayment,
                description=(
                    f"REVERSED payment {repayment.reference} of {repayment.amount} "
                    f"on {repayment.debt.reference}. Reason: {form.cleaned_data['reason']}"
                ),
            )
            messages.success(
                request,
                f"Payment {repayment.reference} reversed. The balance has been restored.",
            )
            return redirect("credit:debt_detail", pk=repayment.debt_id)

    return render(
        request, "credit/repayment_reverse.html", {"form": form, "repayment": repayment}
    )


def debt_write_off(request, pk):
    if not request.user.can_delete_records:
        messages.error(request, "Only an administrator may write off a debt.")
        return redirect("core:forbidden")

    debt = get_owned_or_404(
        DebtRecord.objects.select_related("customer"), request.user, pk=pk
    )
    form = WriteOffForm(request.POST or None)

    if request.method == "POST" and form.is_valid():
        try:
            write_off_debt(debt=debt, user=request.user, reason=form.cleaned_data["reason"])
        except (CreditError, ValidationError) as exc:
            for msg in getattr(exc, "messages", [str(exc)]):
                messages.error(request, msg)
        else:
            log_action(
                AuditAction.OVERRIDE, instance=debt,
                description=(
                    f"WROTE OFF {debt.reference} ({debt.balance} from "
                    f"{debt.customer.name}). Reason: {form.cleaned_data['reason']}"
                ),
            )
            messages.warning(
                request, f"{debt.reference} written off as uncollectable."
            )
            return redirect("credit:debt_detail", pk=debt.pk)

    return render(request, "credit/debt_write_off.html", {"form": form, "debt": debt})


class CreditAccountUpdateView(OwnerScopedMixin, AdminRequiredMixin, UpdateView):
    """Credit limits and blocks are Admin-only financial controls."""

    model = CreditAccount
    form_class = CreditAccountForm
    template_name = "credit/account_form.html"

    def get_success_url(self):
        return self.object.get_absolute_url()

    def form_valid(self, form):
        before = CreditAccount.objects.get(pk=self.object.pk)
        response = super().form_valid(form)
        log_action(
            AuditAction.UPDATE, instance=self.object,
            description=(
                f"Credit terms for {self.object.customer.name}: limit "
                f"{before.credit_limit} -> {self.object.credit_limit}, "
                f"blocked={self.object.is_blocked}."
            ),
        )
        messages.success(self.request, "Credit terms updated.")
        return response


def account_toggle_block(request, pk):
    if not request.user.is_admin:
        messages.error(request, "Administrator privileges are required.")
        return redirect("core:forbidden")

    account = get_owned_or_404(
        CreditAccount.objects.select_related("customer"), request.user, pk=pk
    )

    if request.method == "POST":
        reason = request.POST.get("reason", "").strip()
        new_state = not account.is_blocked
        if new_state and not reason:
            messages.error(request, "Give a reason when blocking credit.")
            return redirect("credit:borrower_detail", pk=account.customer_id)

        set_block(account=account, blocked=new_state, user=request.user, reason=reason)
        log_action(
            AuditAction.OVERRIDE, instance=account,
            description=(
                f"Credit {'BLOCKED' if new_state else 'unblocked'} for "
                f"{account.customer.name}. {reason}"
            ),
        )
        messages.success(
            request,
            f"Credit {'blocked' if new_state else 'unblocked'} for {account.customer.name}.",
        )
        return redirect("credit:borrower_detail", pk=account.customer_id)

    return render(request, "credit/account_block.html", {"account": account})


class AgingReportView(StaffRequiredMixin, TemplateView):
    """Standard accounts-receivable aging report."""

    template_name = "credit/aging_report.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        user = self.request.user
        debts = scoped(DebtRecord.objects.all(), user)
        ctx["aging"] = aging_summary(debts)
        ctx["debts"] = (
            debts.open_debts().select_related("customer").order_by("due_date")
        )
        ctx["by_customer"] = (
            scoped(CreditAccount.objects.in_debt(), user)
            .select_related("customer")
            .order_by("-outstanding_balance")
        )
        return ctx
