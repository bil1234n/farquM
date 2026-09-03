import csv
import datetime as dt

from django.http import HttpResponse
from django.shortcuts import render
from django.utils import timezone
from django.views.generic import TemplateView

from accounts.models import AuditAction
from accounts.services import log_action
from core.mixins import PermissionRequiredMixin, require
from core.scoping import scoped, sees_everything
from credit.models import DebtRecord
from inventory.models import Product, StockMovement
from sales.models import Customer, Transaction

from .dashboards import BLURBS, TITLES, build_cards, build_panels, profile_for
from .selectors import (
    collections_summary,
    daily_series,
    inventory_valuation,
    period_bounds,
    profit_summary,
    receivables_summary,
    sales_by_staff,
    sales_summary,
    top_products,
)


class DashboardView(PermissionRequiredMixin, TemplateView):
    """
    The landing page, laid out for whoever is looking at it.

    The shape of the page comes from reports/dashboards.py, which picks one of
    four layouts from the viewer's permissions and data scope - an owner sees
    margins and a staff league table, a manager sees the shelf and their team,
    a sales assistant sees their own counter. This view's job is to gather the
    figures each layout might want and hand them over; it decides nothing
    about arrangement.

    Cost and profit stay separate permissions, because "may see what the stock
    is worth" and "may see what we make on it" are different amounts of trust -
    a manager buying stock needs the first and not necessarily the second.
    """

    required_permission = "dashboard.view"
    template_name = "reports/dashboard.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        user = self.request.user
        today = timezone.localdate()
        month_start = today.replace(day=1)

        today_stats = sales_summary(today, today, user=user)
        month_stats = sales_summary(month_start, today, user=user)
        receivables = receivables_summary(user=user)

        products = scoped(Product.objects.alive(), user)
        customers = scoped(Customer.objects.all(), user)
        debts = scoped(DebtRecord.objects.all(), user)
        sales = scoped(Transaction.objects.active(), user)

        ctx.update(
            {
                "today": today,
                "today_stats": today_stats,
                "month_stats": month_stats,
                "receivables": receivables,
                "today_collections": collections_summary(today, today, user=user),
                "month_collections": collections_summary(month_start, today, user=user),
                "chart": daily_series(today - dt.timedelta(days=13), today, user=user),
                "recent_sales": (
                    sales.select_related("customer", "sold_by")
                    .order_by("-created_at")[:8]
                ),
                "overdue_debts": (
                    debts.overdue()
                    .select_related("customer")
                    .order_by("due_date")[:8]
                ),
                "low_stock_products": (
                    products.needs_attention().order_by("stock_quantity")[:8]
                ),
                "low_stock_count": products.needs_attention().count(),
                "product_count": products.filter(is_active=True).count(),
                "customer_count": customers.active().count(),
                "debtor_count": customers.with_debt().count(),
                "show_financials": user.can_view_costs,
                "show_costs": user.can_view_costs,
                "show_profit": user.can_view_profit,
                "can_sell": user.has_access("sale.create"),
                "can_see_credit": user.has_access("credit.view"),
                "can_see_products": user.has_access("product.view"),
                # Tells the template whether it is looking at the whole
                # business or one person's slice, so the headings can say so
                # rather than leaving an admin guessing.
                "scope_is_global": sees_everything(user),
            }
        )

        # ---- Profit panels ------------------------------------------------
        if user.can_view_profit:
            ctx["today_profit"] = profit_summary(today, today, user=user)
            ctx["month_profit"] = profit_summary(month_start, today, user=user)

        # ---- Stock valuation ----------------------------------------------
        # Needed by both the cost card and the "potential profit" card, so it
        # is fetched when either permission is held rather than only for cost.
        if user.can_view_costs or user.can_view_profit:
            ctx["valuation"] = inventory_valuation(user=user)

        # ---- Per-person comparison ----------------------------------------
        # Only useful to somebody who can see more than one person's figures,
        # and only worth a table when it holds more than one row.
        if user.data_scope in ("ALL", "TEAM"):
            by_staff = sales_by_staff(month_start, today, user=user)
            if len(by_staff) > 1 or user.data_scope == "ALL":
                ctx["by_manager"] = by_staff

        # ---- The sales assistant's own book -------------------------------
        # Their customers, the ones who owe them money first. Scoped like
        # everything else, so "my customers" really is only theirs.
        if user.has_access("customer.view"):
            ctx["my_customers"] = (
                customers.select_related("credit_account")
                .order_by("-credit_account__outstanding_balance", "name")[:8]
            )

        # ---- Which dashboard is this? -------------------------------------
        profile = profile_for(user)
        ctx["profile"] = profile
        ctx["profile_title"] = TITLES[profile]
        ctx["profile_blurb"] = BLURBS[profile]
        ctx["cards"] = build_cards(user, ctx)
        ctx["panels"] = build_panels(user, profile, ctx)
        return ctx


class SalesReportView(PermissionRequiredMixin, TemplateView):
    """Operational sales report. Cost and margin columns are gated."""

    required_permission = "report.sales"
    template_name = "reports/sales_report.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        start, end = period_bounds(self.request)
        user = self.request.user

        transactions = (
            scoped(Transaction.objects.active(), user)
            .filter(created_at__date__gte=start, created_at__date__lte=end)
            .select_related("customer", "sold_by")
            .order_by("-created_at")
        )

        ctx.update(
            {
                "start": start,
                "end": end,
                "summary": sales_summary(start, end, user=user),
                "chart": daily_series(start, end, user=user),
                "transactions": transactions[:200],
                "transaction_count": transactions.count(),
                "top_products": top_products(
                    start, end, limit=15,
                    include_cost=user.can_view_profit, user=user,
                ),
                "show_financials": user.can_view_profit,
                "show_costs": user.can_view_costs,
                "show_profit": user.can_view_profit,
                "by_staff": sales_by_staff(start, end, user=user),
                "show_staff_table": user.data_scope in ("ALL", "TEAM"),
            }
        )
        return ctx


class ProfitReportView(PermissionRequiredMixin, TemplateView):
    """Cost of goods, gross profit and per-product margins."""

    required_permission = "report.profit"
    template_name = "reports/profit_report.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        start, end = period_bounds(self.request)
        user = self.request.user
        ctx.update(
            {
                "start": start,
                "end": end,
                "profit": profit_summary(start, end, user=user),
                "top_products": top_products(
                    start, end, limit=25, include_cost=True, user=user
                ),
                "valuation": inventory_valuation(user=user),
                "chart": daily_series(start, end, user=user),
            }
        )
        return ctx


class InventoryReportView(PermissionRequiredMixin, TemplateView):
    required_permission = "report.inventory"
    template_name = "reports/inventory_report.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        user = self.request.user
        alive = scoped(Product.objects.alive(), user)
        products = (
            alive.filter(is_active=True)
            .select_related("category")
            .order_by("stock_quantity")
        )
        ctx.update(
            {
                "products": products[:300],
                "product_count": products.count(),
                "low_stock": alive.low_stock().count(),
                "out_of_stock": alive.out_of_stock().count(),
                "recent_movements": (
                    scoped(StockMovement.objects.all(), user)
                    .select_related("product", "performed_by")[:30]
                ),
                "show_financials": user.can_view_costs,
                "show_costs": user.can_view_costs,
            }
        )
        if user.can_view_costs:
            ctx["valuation"] = inventory_valuation(user=user)
        return ctx


class ReceivablesReportView(PermissionRequiredMixin, TemplateView):
    """Accounts receivable / borrower report."""

    required_permission = "report.receivables"
    template_name = "reports/receivables_report.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        user = self.request.user
        receivables = receivables_summary(user=user)
        ctx.update(
            {
                "receivables": receivables,
                "aging": receivables["aging"],
                "debts": (
                    scoped(DebtRecord.objects.all(), user)
                    .open_debts()
                    .select_related("customer", "transaction")
                    .order_by("due_date")[:200]
                ),
                "top_debtors": (
                    scoped(Customer.objects.with_debt(), user)
                    .select_related("credit_account")
                    .order_by("-credit_account__outstanding_balance")[:20]
                ),
            }
        )
        return ctx


# ---------------------------------------------------------------------------
# CSV export
# ---------------------------------------------------------------------------
def export_sales_csv(request):
    """
    CSV export.

    Cost and profit columns are written only for someone who may see them. A
    user without `report.profit` hitting this same URL gets the operational
    columns and nothing else - the filter is on the writer, not on the link.
    """
    blocked = require(
        request, "report.export",
        message="You do not have permission to export data.",
    )
    if blocked:
        return blocked

    start, end = period_bounds(request)
    show_financials = request.user.can_view_profit

    response = HttpResponse(content_type="text/csv")
    response["Content-Disposition"] = (
        f'attachment; filename="sales_{start}_{end}.csv"'
    )

    writer = csv.writer(response)
    header = [
        "Reference", "Date", "Customer", "Phone", "Items", "Subtotal",
        "Discount", "Tax", "Total", "Paid", "Balance", "Status",
        "Method", "Sold by",
    ]
    if show_financials:
        header += ["Cost of goods", "Gross profit", "Margin %"]
    writer.writerow(header)

    transactions = (
        scoped(Transaction.objects.active(), request.user)
        .filter(created_at__date__gte=start, created_at__date__lte=end)
        .select_related("customer", "sold_by")
        .prefetch_related("items")
        .order_by("created_at")
    )

    for txn in transactions:
        row = [
            txn.reference,
            timezone.localtime(txn.created_at).strftime("%Y-%m-%d %H:%M"),
            txn.customer.name if txn.customer else "Walk-in",
            txn.customer.phone if txn.customer else "",
            txn.item_count,
            txn.subtotal, txn.discount_amount, txn.tax_amount,
            txn.total_amount, txn.amount_paid, txn.balance_due,
            txn.get_payment_status_display(),
            txn.get_payment_method_display(),
            txn.sold_by.display_name if txn.sold_by else "",
        ]
        if show_financials:
            row += [txn.total_cost, txn.gross_profit, txn.profit_margin]
        writer.writerow(row)

    log_action(
        AuditAction.EXPORT,
        description=f"Exported sales CSV for {start} to {end} "
                    f"({'with' if show_financials else 'without'} financials).",
        user=request.user,
        request=request,
    )
    return response


def export_receivables_csv(request):
    blocked = require(
        request, "report.export",
        message="You do not have permission to export data.",
    )
    if blocked:
        return blocked

    response = HttpResponse(content_type="text/csv")
    response["Content-Disposition"] = (
        f'attachment; filename="receivables_{timezone.localdate()}.csv"'
    )
    writer = csv.writer(response)
    writer.writerow([
        "Debt ref", "Sale ref", "Customer", "Phone", "Issued", "Due",
        "Principal", "Repaid", "Balance", "Status", "Days overdue", "Aging bucket",
    ])
    for debt in (
        scoped(DebtRecord.objects.all(), request.user)
        .open_debts()
        .select_related("customer", "transaction")
        .order_by("due_date")
    ):
        writer.writerow([
            debt.reference,
            debt.transaction.reference if debt.transaction else "",
            debt.customer.name, debt.customer.phone,
            debt.issued_date, debt.due_date,
            debt.principal, debt.amount_repaid, debt.balance,
            debt.get_status_display(), debt.days_overdue, debt.aging_bucket,
        ])

    log_action(
        AuditAction.EXPORT,
        description="Exported accounts-receivable CSV.",
        user=request.user, request=request,
    )
    return response
