"""
Shared aggregation helpers used by the dashboard and report views.

EVERY function here takes an optional `user`.

    user=None   -> the whole business. Reconciliation tasks and the Django
                   shell want this.
    user=<User> -> scoped through core.scoping: an Admin still sees the whole
                   business, a Manager sees only their own rows.

The default is deliberately "everything", not "nothing", because these are
maths helpers and a silently-empty total is far more dangerous than a loud
KeyError. The callers - views and API endpoints - are responsible for passing
the request user, and they all do.
"""
import datetime as dt
from decimal import Decimal

from django.db.models import Count, DecimalField, ExpressionWrapper, F, Sum
from django.db.models.functions import Coalesce, TruncDate
from django.utils import timezone

from core.scoping import scoped
from core.utils import ZERO, money

DEC = DecimalField(max_digits=16, decimal_places=2)


def cost_expr(price_field="unit_cost", qty_field="quantity"):
    """
    quantity is an integer and unit_cost a decimal, so PostgreSQL needs an
    explicit output type for the product. Without output_field Django raises
    'Expression contains mixed types'.
    """
    return ExpressionWrapper(F(price_field) * F(qty_field), output_field=DEC)


def zero_dec():
    return Coalesce(Sum(ZERO, output_field=DEC), ZERO, output_field=DEC)


def sum_money(queryset, field):
    return money(
        queryset.aggregate(t=Coalesce(Sum(field, output_field=DEC), ZERO, output_field=DEC))["t"]
    )


def _apply_user(queryset, user, path=None):
    """Scope a queryset to `user`, or leave it alone when user is None."""
    if user is None:
        return queryset
    return scoped(queryset, user, path=path)


def period_bounds(request, default_days: int = 30):
    """Parse ?date_from= / ?date_to= into a validated (start, end) pair."""
    today = timezone.localdate()
    raw_from = request.GET.get("date_from", "").strip()
    raw_to = request.GET.get("date_to", "").strip()

    def parse(raw, fallback):
        try:
            return dt.date.fromisoformat(raw)
        except (ValueError, TypeError):
            return fallback

    start = parse(raw_from, today - dt.timedelta(days=default_days))
    end = parse(raw_to, today)
    if start > end:
        start, end = end, start
    return start, end


def sales_summary(start, end, user=None):
    """Headline sales figures for a period, excluding voided documents."""
    from sales.models import Transaction

    qs = _apply_user(Transaction.objects.active(), user).filter(
        created_at__date__gte=start, created_at__date__lte=end
    )
    agg = qs.aggregate(
        revenue=Coalesce(Sum("total_amount", output_field=DEC), ZERO, output_field=DEC),
        collected=Coalesce(Sum("amount_paid", output_field=DEC), ZERO, output_field=DEC),
        outstanding=Coalesce(Sum("balance_due", output_field=DEC), ZERO, output_field=DEC),
        discounts=Coalesce(Sum("discount_amount", output_field=DEC), ZERO, output_field=DEC),
        count=Count("id"),
    )
    return {
        "revenue": money(agg["revenue"]),
        "collected": money(agg["collected"]),
        "outstanding": money(agg["outstanding"]),
        "discounts": money(agg["discounts"]),
        "count": agg["count"],
        "average_sale": money(agg["revenue"] / agg["count"]) if agg["count"] else ZERO,
    }


def cost_of_goods_sold(start, end, user=None) -> Decimal:
    """COGS from the per-line cost snapshots - Admin-facing figure."""
    from sales.models import TransactionItem

    qs = _apply_user(TransactionItem.objects.all(), user).filter(
        transaction__is_voided=False,
        transaction__created_at__date__gte=start,
        transaction__created_at__date__lte=end,
    )
    total = qs.aggregate(
        t=Coalesce(Sum(cost_expr()), ZERO, output_field=DEC)
    )["t"]
    return money(total)


def profit_summary(start, end, user=None):
    """Gross profit for a period. Never shown to a Manager."""
    sales = sales_summary(start, end, user=user)
    cogs = cost_of_goods_sold(start, end, user=user)
    gross = money(sales["revenue"] - cogs)
    margin = money(gross / sales["revenue"] * 100) if sales["revenue"] > ZERO else ZERO
    return {**sales, "cogs": cogs, "gross_profit": gross, "margin_percent": margin}


def daily_series(start, end, user=None):
    """Per-day revenue for the dashboard chart."""
    from sales.models import Transaction

    rows = (
        _apply_user(Transaction.objects.active(), user)
        .filter(created_at__date__gte=start, created_at__date__lte=end)
        .annotate(day=TruncDate("created_at"))
        .values("day")
        .annotate(
            revenue=Coalesce(Sum("total_amount", output_field=DEC), ZERO, output_field=DEC),
            count=Count("id"),
        )
        .order_by("day")
    )
    lookup = {r["day"]: r for r in rows}
    series, cursor = [], start
    while cursor <= end:
        row = lookup.get(cursor)
        series.append(
            {
                "date": cursor,
                "label": cursor.strftime("%d %b"),
                "revenue": money(row["revenue"]) if row else ZERO,
                "count": row["count"] if row else 0,
            }
        )
        cursor += dt.timedelta(days=1)
    return series


def top_products(start, end, limit=10, include_cost=False, user=None):
    from sales.models import TransactionItem

    qs = (
        _apply_user(TransactionItem.objects.all(), user)
        .filter(
            transaction__is_voided=False,
            transaction__created_at__date__gte=start,
            transaction__created_at__date__lte=end,
        )
        .values("product_id", "product_name", "product_sku")
        .annotate(
            units=Coalesce(Sum("quantity"), 0),
            revenue=Coalesce(Sum("line_total", output_field=DEC), ZERO, output_field=DEC),
            cost=Coalesce(Sum(cost_expr()), ZERO, output_field=DEC),
        )
        .order_by("-revenue")[:limit]
    )
    results = []
    for row in qs:
        item = {
            "product_id": row["product_id"],
            "name": row["product_name"],
            "sku": row["product_sku"],
            "units": row["units"],
            "revenue": money(row["revenue"]),
        }
        if include_cost:
            item["cost"] = money(row["cost"])
            item["profit"] = money(row["revenue"] - row["cost"])
            item["margin"] = (
                money(item["profit"] / item["revenue"] * 100)
                if item["revenue"] > ZERO else ZERO
            )
        results.append(item)
    return results


def inventory_valuation(user=None):
    """Stock on hand valued at cost and at retail."""
    from inventory.models import Product

    qs = _apply_user(Product.objects.alive(), user).filter(is_active=True)
    agg = qs.aggregate(
        cost_value=Coalesce(
            Sum(ExpressionWrapper(F("stock_quantity") * F("cost_price"), output_field=DEC)),
            ZERO, output_field=DEC,
        ),
        retail_value=Coalesce(
            Sum(ExpressionWrapper(F("stock_quantity") * F("selling_price"), output_field=DEC)),
            ZERO, output_field=DEC,
        ),
        units=Coalesce(Sum("stock_quantity"), 0),
        skus=Count("id"),
    )
    cost_value = money(agg["cost_value"])
    retail_value = money(agg["retail_value"])
    return {
        "cost_value": cost_value,
        "retail_value": retail_value,
        "potential_profit": money(retail_value - cost_value),
        "units": agg["units"] or 0,
        "skus": agg["skus"],
    }


def receivables_summary(user=None):
    """Current accounts-receivable position."""
    from credit.models import DebtRecord
    from credit.services import aging_summary

    base = _apply_user(DebtRecord.objects.all(), user)
    open_debts = base.open_debts()
    overdue = base.overdue()
    aging = aging_summary(base)
    return {
        "outstanding": sum_money(open_debts, "balance"),
        "debt_count": open_debts.count(),
        "overdue_amount": sum_money(overdue, "balance"),
        "overdue_count": overdue.count(),
        "aging": aging,
    }


def collections_summary(start, end, user=None):
    """
    How much cash actually came IN against old debts during the period.

    Distinct from sales_summary()["collected"], which only counts money taken
    at the till at the moment of sale. A shop living on credit collects most
    of its money days or weeks later, and without this figure the dashboard
    makes a healthy week look dead.
    """
    from credit.models import Repayment

    qs = _apply_user(
        Repayment.objects.filter(is_reversed=False), user
    ).filter(paid_at__date__gte=start, paid_at__date__lte=end)
    return {
        "collected": sum_money(qs, "amount"),
        "count": qs.count(),
    }


def sales_by_staff(start, end, user=None):
    """
    Revenue broken down by the manager who recorded it.

    Only meaningful for an Admin - a Manager scoped to their own rows sees a
    single line that is just their own total again - so callers gate it on
    the role rather than showing everyone a table of one.
    """
    from sales.models import Transaction

    rows = (
        _apply_user(Transaction.objects.active(), user)
        .filter(created_at__date__gte=start, created_at__date__lte=end)
        .values("owner_id", "owner__username", "owner__first_name", "owner__last_name")
        .annotate(
            count=Count("id"),
            revenue=Coalesce(Sum("total_amount", output_field=DEC), ZERO, output_field=DEC),
            collected=Coalesce(Sum("amount_paid", output_field=DEC), ZERO, output_field=DEC),
            outstanding=Coalesce(Sum("balance_due", output_field=DEC), ZERO, output_field=DEC),
        )
        .order_by("-revenue")
    )
    results = []
    for row in rows:
        full = f"{row['owner__first_name'] or ''} {row['owner__last_name'] or ''}".strip()
        results.append(
            {
                "owner_id": row["owner_id"],
                "name": full or row["owner__username"] or "Unassigned",
                "count": row["count"],
                "revenue": money(row["revenue"]),
                "collected": money(row["collected"]),
                "outstanding": money(row["outstanding"]),
            }
        )
    return results
