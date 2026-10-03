import csv
import datetime as dt

from django.http import HttpResponse
from django.shortcuts import render
from django.utils import timezone
from django.views.generic import TemplateView

from accounts.models import AuditAction
from accounts.services import log_action
from core.mixins import PermissionRequiredMixin, require
from core.permissions import HANDOVER_QUEUE
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
    running_split,
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

        # ---- The yard: goods sold but not yet collected --------------------
        # Scoped like everything else: the stock keeper, who sees every sale,
        # sees the whole yard; a seller sees their own customers' goods.
        if user.has_access(*HANDOVER_QUEUE):
            from sales.delivery import queue_summary

            ctx["delivery_queue"] = queue_summary(user)

        # ---- What the business spent this month ---------------------------
        if user.has_access("expense.view"):
            from expenses.models import Expense
            from expenses.services import running_costs, summarize

            spent = summarize(
                scoped(Expense.objects.all(), user).filter(
                    spent_on__gte=month_start, spent_on__lte=today
                )
            )
            top = spent["total"]
            for row in spent["by_category"]:
                row["percent"] = round(row["total"] * 100 / top) if top else 0
            spent["by_category"] = spent["by_category"][:5]
            ctx["month_expenses"] = spent
            month_profit = ctx.get("month_profit")
            if month_profit is not None:
                # Gross profit less what it cost to run the place: the number
                # an owner actually means by "did we make money this month".
                # A batch's own costs are already inside the cost of the goods
                # sold (they went into its cost per unit), so they are taken
                # off once, there - see expenses.services.running_costs. So is
                # the share the owner's own costs hold (running_split).
                _, taken = running_split(running_costs(spent), month_profit["covered"])
                ctx["month_net_profit"] = month_profit["gross_profit"] - taken

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


class AuditView(PermissionRequiredMixin, TemplateView):
    """
    The Audit: what went in, what came back, what is on hand, and what a
    product really costs. The figures come from reports/audit.py, which the
    phone's API uses too; the charts are drawn here as SVG (reports/charts.py).
    """

    required_permission = "costing.view"
    template_name = "reports/audit.html"

    def get_context_data(self, **kwargs):
        from django.utils.http import urlencode

        from .audit import RANGES, build_report, resolve_period
        from .charts import (
            BAD, GOOD, LINE_PRICE, LINE_SUGGESTED, LINE_YOURS, SERIES,
            candle_svg, donut_svg, legend,
        )

        ctx = super().get_context_data(**kwargs)
        user = self.request.user
        q = self.request.GET
        period = resolve_period(
            q.get("range", ""), q.get("date_from", ""), q.get("date_to", ""), user=user
        )
        report = build_report(user, period, product_id=q.get("product"))

        chosen = next(
            (r for r in report["costing"] if r["id"] == report["cost_product_id"]), None
        )
        cost_lines = []
        if chosen:
            cost_lines = [
                ("Your cost", chosen["your_cost"], LINE_YOURS),
                ("Selling price", chosen["selling_price"], LINE_PRICE),
                ("Suggested", chosen["suggested"], LINE_SUGGESTED),
            ]

        spend = report["money_out"]
        spend_parts = [
            ("Materials and stock", spend["kinds"][0]["amount"]),
            ("Wages", spend["kinds"][1]["amount"]),
            ("Running costs", spend["kinds"][2]["amount"]),
        ]
        held = report["holdings"]
        held_parts = [
            ("Raw materials", held["materials_value"]),
            ("Finished products", held["products_value"]),
            ("Owed by customers", held["owed"]),
        ]
        top_category = max((c["total"] for c in spend["categories"]), default=0)

        ctx.update({
            "report": report,
            "period": period,
            "ranges": list(RANGES),
            "chosen": chosen,
            "cash_chart": candle_svg(
                report["cash_candles"], bucket=period.bucket, rising_good=True,
                zero_line=True, skip_idle=True, title="Money in minus money out",
            ),
            "cash_chart_narrow": candle_svg(
                report["cash_candles"], bucket=period.bucket, rising_good=True,
                zero_line=True, skip_idle=True, title="Money in minus money out",
                width=360, height=230, max_labels=4,
            ),
            "cost_chart": candle_svg(
                report["cost_candles"], bucket=period.bucket, rising_good=False,
                lines=[(lbl, v, c) for lbl, v, c in cost_lines if v is not None],
                title="Batch cost per unit",
            ) if chosen else "",
            "cost_chart_narrow": candle_svg(
                report["cost_candles"], bucket=period.bucket, rising_good=False,
                lines=[(lbl, v, c) for lbl, v, c in cost_lines if v is not None],
                title="Batch cost per unit", width=360, height=230, max_labels=4,
            ) if chosen else "",
            "cost_lines": [(lbl, v, c) for lbl, v, c in cost_lines if v is not None],
            "spend_donut": donut_svg(
                spend_parts, centre_value=_compact_money(spend["total"]),
                centre_label="spent", title="Where the money went",
            ),
            "spend_legend": legend(spend_parts),
            "held_donut": donut_svg(
                held_parts, centre_value=_compact_money(held["total"]),
                centre_label="on hand", title="What the business holds",
            ),
            "held_legend": legend(held_parts),
            "top_category": top_category,
            "good_colour": GOOD,
            "bad_colour": BAD,
            "series": SERIES,
            "query": {k: v for k, v in q.items() if k in ("range", "date_from", "date_to")},
            # For the four cards, which open their own pages for the same period.
            "audit_query": urlencode(
                {k: v for k, v in q.items() if k in ("range", "date_from", "date_to") and v}
            ),
        })
        return ctx


def _compact_money(value):
    from .charts import compact

    return compact(value)


class AuditDetailView(PermissionRequiredMixin, TemplateView):
    """
    One Audit card opened up - /reports/audit/money-out/ and its three
    siblings. The figures come from reports/audit_detail.py, which the phone
    uses too, and each page's total is its card's total.
    """

    required_permission = "costing.view"
    template_name = "reports/audit_detail.html"

    HEADINGS = {
        "money-out": "Money out",
        "money-in": "Money in",
        "profit": "Profit after all costs",
        "on-hand": "On hand now",
    }
    #: The keys of the bars and rings, as the page says them.
    KEY_LABELS = {
        "materials": "Materials and stock",
        "wages": "Wages",
        "running": "Running costs",
        "till": "Paid at the till",
        "repaid": "Debts paid off",
        "profit": "Profit",
        "products": "Finished products",
        "owed": "Owed by customers",
    }
    #: What each line of a list is.
    TYPE_LABELS = {
        "delivery": "Delivery received",
        "returned": "Returned to supplier",
        "opening": "Opening balance",
        "stock": "Stock bought",
        "stock_returned": "Stock returned to supplier",
        "stock_opening": "Opening stock",
        "expense": "Expense",
        "wage": "Staff payment",
        "sale": "Sale",
        "repayment": "Debt payment",
    }

    def get(self, request, *args, **kwargs):
        from django.http import Http404

        from .audit_detail import KINDS

        if kwargs.get("kind") not in KINDS:
            raise Http404("No such Audit page.")
        return super().get(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        from django.utils.http import urlencode

        from .audit import RANGES, resolve_period
        from .audit_detail import build_detail
        from .charts import BAD, GOOD, SERIES, bars_svg, donut_svg, legend

        ctx = super().get_context_data(**kwargs)
        kind = kwargs["kind"]
        q = self.request.GET
        period = resolve_period(
            q.get("range", ""), q.get("date_from", ""), q.get("date_to", ""),
            user=self.request.user,
        )
        # The list of payments can be searched (reports.audit_detail.filter_lines).
        detail = build_detail(self.request.user, period, kind, filters=q)

        # Bars, in the order of the detail's keys (which is the colour order).
        keys = detail.get("series_keys") or []
        labels = [self.KEY_LABELS.get(k, k) for k in keys]
        signed = kind == "profit"
        chart = chart_narrow = ""
        if detail.get("series"):
            chart = bars_svg(
                detail["series"], labels=labels, signed=signed, bucket=period.bucket,
                title=self.HEADINGS[kind],
            )
            chart_narrow = bars_svg(
                detail["series"], labels=labels, signed=signed, bucket=period.bucket,
                title=self.HEADINGS[kind], width=360, height=220, max_labels=4,
            )
        series_legend = (
            [{"label": "Profit", "colour": GOOD}, {"label": "Loss", "colour": BAD}]
            if signed else
            [{"label": label, "colour": SERIES[i % len(SERIES)]} for i, label in enumerate(labels)]
        )

        ring = ring_legend = ""
        parts = [
            (self.KEY_LABELS.get(p["key"], p["key"]), p["amount"])
            for p in detail.get("parts") or []
        ]
        if kind == "on-hand":
            parts[0] = ("Raw materials", parts[0][1])
        if parts:
            ring = donut_svg(
                parts, centre_value=_compact_money(detail["total"]),
                centre_label="total", title=self.HEADINGS[kind],
            )
            ring_legend = legend(parts)

        def shares(rows, field):
            top = max((r[field] for r in rows), default=0)
            return [
                {**r, "share": round(float(r[field]) / float(top) * 100) if top and r[field] > 0 else 0}
                for r in rows
            ]

        for row in detail.get("items") or []:
            row["type_label"] = self.TYPE_LABELS.get(row["type"], row["type"])
        for row in (detail.get("not_counted") or {}).get("items") or []:
            row["type_label"] = self.TYPE_LABELS.get(row["type"], row["type"])

        query = {k: v for k, v in q.items() if k in ("range", "date_from", "date_to") and v}
        ctx.update({
            "kind": kind,
            "heading": self.HEADINGS[kind],
            "detail": detail,
            "period": period,
            "ranges": list(RANGES),
            "chart": chart,
            "chart_narrow": chart_narrow,
            "series_legend": series_legend,
            "key_labels": labels,
            "ring": ring,
            "ring_legend": ring_legend,
            "categories": shares(detail.get("categories") or [], "total"),
            "running_categories": shares(detail.get("running_categories") or [], "total"),
            "by_person": shares(detail.get("by_person") or [], "amount"),
            "query": query,
            "audit_query": urlencode(query),
            "series_colours": SERIES,
            "good_colour": GOOD,
            "bad_colour": BAD,
        })
        return ctx


def audit_set_cost(request, pk):
    """
    The owner's figure for one product, from the Audit page. POST only; the
    answer is the page again, scrolled back to the product.
    """
    from django.contrib import messages
    from django.shortcuts import get_object_or_404, redirect
    from django.urls import reverse
    from django.utils.http import urlencode

    from .audit import CostError, set_your_cost

    blocked = require(
        request, "costing.set",
        message="Only the owner can set what a product costs.",
    )
    if blocked:
        return blocked
    if request.method != "POST":
        return redirect("reports:audit")

    product = get_object_or_404(scoped(Product.objects.alive(), request.user), pk=pk)
    raw = request.POST.get("cost", "")
    clearing = "clear" in request.POST
    try:
        set_your_cost(
            product, None if clearing else raw, user=request.user,
            note=request.POST.get("note", ""), request=request,
        )
    except CostError as exc:
        messages.error(request, str(exc))
    else:
        if clearing:
            messages.success(request, f"Your cost for {product.name} was cleared.")
        else:
            messages.success(request, f"Saved: one {product.name} costs {product.audit_cost}.")

    keep = {k: v for k, v in request.POST.items() if k in ("range", "date_from", "date_to") and v}
    keep["product"] = product.pk
    return redirect(f"{reverse('reports:audit')}?{urlencode(keep)}#cost")


def audit_correct(request, source, pk):
    """
    Put right a delivery or restock entered wrong - from the Money out page.
    GET shows it with the form; POST corrects it and goes back to the list
    (reports/corrections.py has the rules).
    """
    from django.contrib import messages
    from django.http import Http404
    from django.shortcuts import redirect
    from django.urls import reverse
    from django.utils.http import url_has_allowed_host_and_scheme

    from core.models import coded_label
    from inventory.models import StockMovement
    from production.models import MaterialMovement, MaterialUnit

    from .corrections import SOURCES, CorrectionError, can_correct, correct_delivery

    blocked = require(request, "costing.view")
    if blocked:
        return blocked
    if source not in SOURCES:
        raise Http404("No such delivery.")
    if not can_correct(request.user, source):
        messages.error(request, "You may not correct deliveries.")
        return redirect("reports:audit_detail", kind="money-out")

    Ledger = MaterialMovement if source == "material" else StockMovement
    row = (
        Ledger.objects.select_related(source, "performed_by", "corrected_by")
        .filter(pk=pk, movement_type=SOURCES[source]["movement_type"]).first()
    )
    item = getattr(row, source, None) if row else None
    if row is None or not scoped(type(item).objects.filter(pk=item.pk), request.user).exists():
        raise Http404("No such delivery.")

    back = request.POST.get("next") or request.GET.get("next") or ""
    if not back.startswith("/reports/audit/") or not url_has_allowed_host_and_scheme(
        back, allowed_hosts={request.get_host()}
    ):
        back = reverse("reports:audit_detail", kwargs={"kind": "money-out"}) + "#payments"

    error = ""
    if request.method == "POST":
        never = "never" in request.POST
        try:
            correct_delivery(
                source, row.pk, user=request.user,
                quantity="0" if never else request.POST.get("quantity", ""),
                unit_cost=request.POST.get("unit_cost") or None,
                note=request.POST.get("note", ""),
                change_stock=bool(request.POST.get("change_stock")),
                request=request,
            )
        except CorrectionError as exc:
            error = str(exc)
        else:
            if never:
                messages.success(request, "Marked as never happened. It no longer counts as money out.")
            else:
                messages.success(request, "Delivery corrected.")
            return redirect(back)

    unit = (
        coded_label("MATERIAL_UNIT", item.unit, MaterialUnit.choices)
        if source == "material" else item.get_unit_display()
    )
    store = item.quantity_in_stock if source == "material" else item.stock_quantity
    return render(request, "reports/audit_correct.html", {
        "row": row, "item": item, "source": source, "unit": unit, "store": store,
        "counted_quantity": row.counted_quantity, "counted_unit_cost": row.counted_unit_cost,
        "back": back, "error": error,
        "posted": request.POST if request.method == "POST" else None,
    })


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
