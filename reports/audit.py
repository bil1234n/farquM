"""
The Audit - the whole business on one page.

WHAT IT ANSWERS
---------------
The owner's questions, in his own words:

  * How much have I put in?            money out: expenses, raw materials
                                       bought, stock bought, opening stock
  * How much came back?                money in: paid at the till, and debts
                                       paid off later
  * What did I make?                   profit after every cost
  * What do I still have?              raw materials and finished goods on
                                       hand, and what customers still owe
  * What does one product really cost? the owner's own figure, with the
                                       numbers to make it from

THE ONE NUMBER NOBODY CAN MEASURE
---------------------------------
A batch knows what its materials cost and what was paid against it. It does
not know its share of today's electricity or this week's wages - those change
from day to day and belong to no single batch. So what one unit costs is, in
the end, a judgement: the owner looks at everything and decides.

The Audit lays out what can be measured - materials and batch costs per unit,
batch by batch, drawn as candles so the swings show - and a suggestion for the
rest: the period's running costs shared out in proportion to the materials
each product used. The owner then writes down his figure (Product.audit_cost)
and every per-product margin here is worked out from it.

TWO PROFITS, ON PURPOSE
-----------------------
"Profit after all costs" is sales, less the recorded cost of what was sold
(materials and batch costs, copied onto each sale line when it was made), less
the period's running costs. It needs no guesses and matches the Profit report.

"Profit at your costs" is sales less units sold times the owner's figure. It is
what the owner's own numbers say the products made. Running costs are NOT
taken off it as well - his figure already includes them, and taking them off
twice would count the electricity twice. When the two profits agree over a
long period his costs are right; when "at your costs" sits well above, his
figures are missing something. That is the most useful thing the Audit can
tell him, so it says so in plain words (see `_insights`).

SCOPE
-----
Every queryset goes through core.scoping, like every other report: the owner
sees the whole business; a manager sees the shared stock and production plus
his own team's sales and expenses.
"""
import datetime as dt
from collections import defaultdict
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

from django.db.models import Count, DecimalField, ExpressionWrapper, F, Min, Sum
from django.db.models.functions import Coalesce
from django.utils import timezone

from core.scoping import scoped
from core.utils import ZERO, money

DEC = DecimalField(max_digits=18, decimal_places=4)

#: The range buttons, in the order they are offered. None = since the start.
RANGES: dict[str, int | None] = {"30d": 30, "90d": 90, "365d": 365, "all": None}
DEFAULT_RANGE = "90d"

#: Money-out kinds and stock kinds, in the order (and so the colour) the
#: charts draw them. Three each: three colours stay apart for every reader,
#: including the colour-blind, whichever two of them end up side by side.
SPEND_KINDS = ("materials", "wages", "running")
HOLDING_KINDS = ("materials", "products", "owed")

#: How far the owner's figure may sit from the suggestion before it is
#: worth a word. Below this, the difference is noise in the electricity bill.
ESTIMATE_TOLERANCE = Decimal("0.10")

#: Insights shown at most, most serious first.
MAX_INSIGHTS = 8

LEVEL_ORDER = {"critical": 0, "warning": 1, "good": 2, "info": 3}


# ---------------------------------------------------------------------------
# The period
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Period:
    start: dt.date
    end: dt.date
    key: str  # one of RANGES, or "custom"

    @property
    def days(self) -> int:
        return (self.end - self.start).days + 1

    @property
    def bucket(self) -> str:
        """How wide one candle is: a day, a week, or a month."""
        if self.days <= 31:
            return "day"
        if self.days <= 186:
            return "week"
        return "month"

    def previous(self) -> "Period":
        """The same number of days immediately before, for comparison."""
        end = self.start - dt.timedelta(days=1)
        return Period(end - dt.timedelta(days=self.days - 1), end, "previous")


def _parse_date(raw):
    try:
        return dt.date.fromisoformat(str(raw or "").strip())
    except ValueError:
        return None


def resolve_period(range_key="", date_from="", date_to="", user=None, today=None) -> Period:
    """
    The period asked for. Custom dates win over a range button; an unknown
    range falls back to the default rather than refusing - this comes off a
    URL somebody may have bookmarked months ago.
    """
    today = today or timezone.localdate()
    start, end = _parse_date(date_from), _parse_date(date_to)
    if start or end:
        end = end or today
        start = start or end - dt.timedelta(days=29)
        if start > end:
            start, end = end, start
        # Nothing has happened tomorrow yet; a range into the future would
        # only draw empty candles.
        end = min(end, today)
        start = min(start, end)
        return Period(start, end, "custom")

    key = range_key if range_key in RANGES else DEFAULT_RANGE
    days = RANGES[key]
    if days is None:
        return Period(first_activity(user) or today, today, "all")
    return Period(today - dt.timedelta(days=days - 1), today, key)


def first_activity(user):
    """The first day anything was recorded - where "since the start" begins."""
    from expenses.models import Expense
    from inventory.models import StockMovement
    from production.models import MaterialMovement, ProductionRun
    from sales.models import Transaction

    def first(qs, field):
        value = qs.aggregate(v=Min(field))["v"]
        if isinstance(value, dt.datetime):
            return timezone.localtime(value).date()
        return value

    candidates = [
        first(scoped(Transaction.objects.active(), user), "created_at"),
        first(scoped(Expense.objects.active(), user), "spent_on"),
        first(scoped(MaterialMovement.objects.all(), user), "created_at"),
        first(scoped(StockMovement.objects.all(), user), "created_at"),
        first(scoped(ProductionRun.objects.completed(), user), "produced_on"),
    ]
    found = [c for c in candidates if c]
    return min(found) if found else None


# ---------------------------------------------------------------------------
# Buckets and candles
# ---------------------------------------------------------------------------
def bucket_of(day: dt.date, bucket: str) -> dt.date:
    if bucket == "day":
        return day
    if bucket == "week":
        return day - dt.timedelta(days=day.weekday())
    return day.replace(day=1)


def buckets(period: Period) -> list[tuple[dt.date, dt.date]]:
    """Every bucket in the period, first and last clipped to it."""
    out = []
    cursor = bucket_of(period.start, period.bucket)
    while cursor <= period.end:
        if period.bucket == "day":
            nxt = cursor + dt.timedelta(days=1)
        elif period.bucket == "week":
            nxt = cursor + dt.timedelta(days=7)
        else:
            nxt = (cursor.replace(day=28) + dt.timedelta(days=4)).replace(day=1)
        out.append((max(cursor, period.start), min(nxt - dt.timedelta(days=1), period.end)))
        cursor = nxt
    return out


def _moment(value) -> dt.datetime:
    """A naive local datetime, so a sale at 10:00 sorts after an expense dated that day."""
    if isinstance(value, dt.datetime):
        if timezone.is_aware(value):
            value = timezone.localtime(value)
        return value.replace(tzinfo=None)
    return dt.datetime.combine(value, dt.time.min)


def running_candles(events, period: Period) -> list[dict]:
    """
    Candles of a running total: money in minus money out since the period
    began. Each candle opens where the last one closed; its wick spans the
    lowest and highest the total reached inside the bucket, in the order the
    money actually moved.
    """
    events = sorted(events, key=lambda e: e[0])
    out, running, i = [], ZERO, 0
    for start, end in buckets(period):
        opened = high = low = running
        came_in = went_out = ZERO
        while i < len(events) and events[i][0].date() <= end:
            amount = events[i][1]
            running += amount
            high, low = max(high, running), min(low, running)
            if amount >= 0:
                came_in += amount
            else:
                went_out -= amount
            i += 1
        out.append({
            "start": start, "end": end,
            "open": money(opened), "high": money(high), "low": money(low),
            "close": money(running),
            "money_in": money(came_in), "money_out": money(went_out),
        })
    return out


def value_candles(points, period: Period) -> list[dict]:
    """
    Candles of separate readings - batch costs, one per batch. A bucket with
    no batch has no candle (open is None), rather than a flat one pretending
    a cost was measured.
    """
    grouped = defaultdict(list)
    for day, value in sorted(points, key=lambda p: p[0]):
        grouped[bucket_of(day, period.bucket)].append(value)
    out = []
    for start, end in buckets(period):
        values = grouped.get(bucket_of(start, period.bucket), [])
        if not values:
            out.append({"start": start, "end": end, "open": None, "high": None,
                        "low": None, "close": None, "count": 0})
            continue
        out.append({
            "start": start, "end": end,
            "open": money(values[0]), "high": money(max(values)),
            "low": money(min(values)), "close": money(values[-1]),
            "count": len(values),
        })
    return out


# ---------------------------------------------------------------------------
# Money out
# ---------------------------------------------------------------------------
def _sum(qs, expr):
    return qs.aggregate(t=Coalesce(Sum(expr, output_field=DEC), ZERO, output_field=DEC))["t"]


def _names(ids) -> dict:
    """Display names for a handful of user ids, in one query."""
    from accounts.models import User

    ids = {i for i in ids if i}
    if not ids:
        return {}
    return {u.pk: u.display_name for u in User.objects.filter(pk__in=ids)}


def _left_in_deleted(user):
    """
    Deliveries whose goods were deleted with the item before anybody used them.

    Deleting a raw material - or a product - that still has stock takes that
    stock out of the business without a write-off: it is not on hand any more,
    it was not used and it was not sold. Nearly always the item itself was the
    mistake (a test entry, a duplicate), and so were its deliveries. Counted as
    money out, they made the Audit say the owner had spent hundreds of
    thousands he never spent.

    So the stock left in a deleted item is traced back to the deliveries that
    brought it in - newest first, because the last in is what was still there -
    and that much of them is not counted. An item that was used up and only
    then deleted had nothing left, so every delivery of it still counts: that
    money really was spent. (Stock that is really lost should be written off
    before the item is deleted; then it counts, as a loss.)

    Returns two maps of {movement id: quantity not counted}: materials, products.
    """
    from inventory.models import MovementType, Product, StockMovement
    from production.models import MaterialMovement, MaterialMovementType, RawMaterial

    def trace(left, movements, counted_kinds):
        found = {}
        for pk, kind, delta in movements:
            if left <= 0:
                break
            take = min(left, delta)
            if kind in counted_kinds:
                found[pk] = take
            left -= take
        return found

    materials = {}
    for m in scoped(
        RawMaterial.objects.filter(is_deleted=True, quantity_in_stock__gt=0), user
    ):
        materials.update(trace(
            m.quantity_in_stock,
            MaterialMovement.objects.filter(material=m, quantity_delta__gt=0)
            .order_by("-created_at", "-id")
            .values_list("pk", "movement_type", "quantity_delta"),
            {MaterialMovementType.PURCHASE, MaterialMovementType.OPENING},
        ))
    products = {}
    for p in scoped(Product.objects.filter(is_deleted=True, stock_quantity__gt=0), user):
        products.update(trace(
            p.stock_quantity,
            StockMovement.objects.filter(product=p, quantity_delta__gt=0)
            .order_by("-created_at", "-id")
            .values_list("pk", "movement_type", "quantity_delta"),
            {MovementType.RESTOCK, MovementType.OPENING},
        ))
    return materials, products


#: Ledger rows that are money out, and what each is called on the page.
MATERIAL_OUT_TYPES = {"PURCHASE": "delivery", "RETURN_OUT": "returned", "OPENING": "opening"}
STOCK_OUT_TYPES = {"RESTOCK": "stock", "RETURN_OUT": "stock_returned", "OPENING": "stock_opening"}


def _out_items(user, period: Period):
    """
    Every payment out in the period, one dict each, oldest first.

    The Audit card adds these up and the Money out page lists them - from this
    one function, so the card and the page can never disagree. Returns
    (items, not_counted): the second is what _left_in_deleted left out, so the
    page can say what was left out and why.

    `amount` is positive for money out and negative for money back (goods
    returned to a supplier), unrounded; the totals round once, at the end.
    """
    from core.models import coded_label
    from expenses.models import Expense
    from inventory.models import Product, StockMovement
    from production.models import MaterialMovement, MaterialUnit

    start, end = period.start, period.end
    skip_materials, skip_products = _left_in_deleted(user)

    expenses = list(scoped(Expense.objects.between(start, end), user).values(
        "id", "reference", "spent_on", "amount", "category_name", "payee", "notes",
        "employee_id", "employee__name", "production_run_id", "recorded_by_id",
    ))
    deliveries = list(scoped(
        MaterialMovement.objects.filter(
            movement_type__in=list(MATERIAL_OUT_TYPES),
            created_at__date__gte=start, created_at__date__lte=end,
        ),
        user,
    ).values(
        "id", "created_at", "movement_type", "quantity_delta", "unit_cost", "reference",
        "performed_by_id", "material_id", "material__name", "material__unit",
        "material__is_deleted",
    ))
    stock = list(scoped(
        StockMovement.objects.filter(
            movement_type__in=list(STOCK_OUT_TYPES),
            created_at__date__gte=start, created_at__date__lte=end,
        ),
        user,
    ).values(
        "id", "created_at", "movement_type", "quantity_delta", "unit_cost", "reference",
        "performed_by_id", "product_id", "product__name", "product__unit",
        "product__is_deleted", "product__cost_price",
    ))
    names = _names(
        [r["recorded_by_id"] for r in expenses]
        + [r["performed_by_id"] for r in deliveries]
        + [r["performed_by_id"] for r in stock]
    )

    items, not_counted = [], []
    for r in expenses:
        staff = bool(r["employee_id"])
        items.append({
            "id": r["id"], "moment": _moment(r["spent_on"]), "day": r["spent_on"],
            "has_time": False,
            "kind": "wages" if staff else "running",
            "type": "wage" if staff else "expense",
            "amount": r["amount"] or ZERO,
            "title": r["category_name"] or "",
            "party": r["employee__name"] or r["payee"] or "",
            "note": (r["notes"] or "").strip()[:160],
            "reference": r["reference"] or "",
            "by": names.get(r["recorded_by_id"], ""),
            "in_batch": bool(r["production_run_id"]),
            "target": None, "quantity": None, "unit_display": "", "unit_cost": None,
            "deleted": False,
        })

    def ledger_rows(rows, kinds, skips, target, unit_group, unit_choices, fallback_cost=None):
        for r in rows:
            moment = _moment(r["created_at"])
            cost = r["unit_cost"]
            if cost is None and fallback_cost:
                cost = r[fallback_cost]
            cost = cost or ZERO
            delta = Decimal(r["quantity_delta"] or 0)
            base = {
                "id": r["id"], "moment": moment, "day": moment.date(), "has_time": True,
                "kind": "materials", "type": kinds[r["movement_type"]],
                "title": r[f"{target}__name"] or "",
                "party": "", "note": "",
                "reference": r["reference"] or "",
                "by": names.get(r["performed_by_id"], ""),
                "in_batch": False,
                "target": r[f"{target}_id"],
                "unit_display": coded_label(unit_group, r[f"{target}__unit"], unit_choices),
                "unit_cost": cost,
                "deleted": bool(r[f"{target}__is_deleted"]),
            }
            skipped = Decimal(skips.get(r["id"], 0))
            if skipped:
                not_counted.append({**base, "quantity": skipped, "amount": skipped * cost})
            counted = delta - skipped
            if counted:
                items.append({**base, "quantity": counted, "amount": counted * cost})

    ledger_rows(deliveries, MATERIAL_OUT_TYPES, skip_materials, "material",
                "MATERIAL_UNIT", MaterialUnit.choices)
    ledger_rows(stock, STOCK_OUT_TYPES, skip_products, "product",
                "PRODUCT_UNIT", Product.Unit.choices, fallback_cost="product__cost_price")

    items.sort(key=lambda i: (i["moment"], i["id"]))
    not_counted.sort(key=lambda i: (i["moment"], i["id"]))
    return items, not_counted


def _out_totals(items, not_counted=()) -> dict:
    """The Audit card's figures, from _out_items."""
    wages = running = in_batches = materials = stock_bought = opening = ZERO
    by_category = defaultdict(lambda: [ZERO, 0])
    for i in items:
        amount = i["amount"]
        if i["type"] in ("expense", "wage"):
            if i["kind"] == "wages":
                wages += amount
            else:
                running += amount
            if i["in_batch"]:
                in_batches += amount
            row = by_category[i["title"] or "-"]
            row[0] += amount
            row[1] += 1
        elif i["type"] in ("opening", "stock_opening"):
            opening += amount
        elif i["type"] in ("delivery", "returned"):
            materials += amount  # a return to the supplier is negative
        else:
            stock_bought += amount

    expense_total = wages + running
    materials_and_stock = materials + stock_bought + opening
    total = expense_total + materials_and_stock
    categories = sorted(
        ({"label": label, "total": money(t), "count": n} for label, (t, n) in by_category.items()),
        key=lambda r: (-r["total"], r["label"]),
    )
    return {
        "total": money(total),
        "expenses": money(expense_total),
        "wages": money(wages),
        "running": money(running),
        "in_batches": money(in_batches),
        "materials": money(materials),
        "stock_bought": money(stock_bought),
        "opening": money(opening),
        # Finished goods bought in, and stock the business already had.
        "bought_in": money(stock_bought + opening),
        "kinds": [
            {"key": "materials", "amount": money(materials_and_stock)},
            {"key": "wages", "amount": money(wages)},
            {"key": "running", "amount": money(running)},
        ],
        "categories": categories,
        # Deliveries of items deleted with their stock still in them.
        "not_counted": money(sum((i["amount"] for i in not_counted), ZERO)),
        "not_counted_count": len(not_counted),
    }


def money_out(user, period: Period, with_events=False):
    """
    Everything that left the business, by kind.

    Raw materials count when they are BOUGHT, not when a batch uses them: a
    bag of cement is money spent the day it arrives, and counting it again
    when it is mixed would spend it twice. Opening balances count too - stock
    the business already had is money the owner put in before the first sale.
    Deliveries of an item that was deleted with the goods still in it do not
    (see _left_in_deleted).
    """
    items, not_counted = _out_items(user, period)
    result = _out_totals(items, not_counted)
    if not with_events:
        return result
    return result, [(i["moment"], -i["amount"]) for i in items if i["amount"]]


# ---------------------------------------------------------------------------
# Money in
# ---------------------------------------------------------------------------
#: Settled without money changing hands - a debt cleared by taking the goods
#: back, or written off with a repayment row. Neither is money that came in.
NOT_CASH_METHODS = ("GOODS_RETURN", "WRITE_OFF")


def _in_items(user, period: Period):
    """
    Every payment in during the period, one dict each, oldest first: what was
    paid at the till for each sale, and each debt repayment, on the day the
    money came. The Audit card adds them up; the Money in page lists them.

    A sale's amount_paid is NOT used as it stands: it grows as the customer
    pays the debt off, so a sale from March would carry May's repayment back
    into March, and the repayment would be counted a second time in May.
    What was paid at the till is the total less what went on credit.
    """
    from credit.models import Repayment
    from sales.models import PaymentMethod, Transaction

    start, end = period.start, period.end
    sales = list(scoped(
        Transaction.objects.active().filter(
            created_at__date__gte=start, created_at__date__lte=end
        ),
        user,
    ).values(
        "id", "reference", "created_at", "total_amount", "amount_paid",
        "debt_record__principal", "customer__name", "customer_name_snapshot",
        "sold_by_id", "payment_method",
    ))
    repayments = list(scoped(
        Repayment.objects.filter(
            is_reversed=False, paid_at__date__gte=start, paid_at__date__lte=end
        ).exclude(method__in=NOT_CASH_METHODS),
        user,
    ).values(
        "id", "reference", "paid_at", "amount", "method", "debt_id",
        "debt__reference", "debt__customer__name", "received_by_id",
    ))
    names = _names(
        [r["sold_by_id"] for r in sales] + [r["received_by_id"] for r in repayments]
    )
    sale_methods = dict(PaymentMethod.choices)
    repay_methods = dict(Repayment.Method.choices)

    items = []
    for r in sales:
        principal = r["debt_record__principal"]
        total = r["total_amount"] or ZERO
        amount = (total - principal) if principal is not None else (r["amount_paid"] or ZERO)
        amount = max(amount, ZERO)
        moment = _moment(r["created_at"])
        items.append({
            "id": r["id"], "moment": moment, "day": moment.date(), "has_time": True,
            "kind": "till", "type": "sale", "amount": amount,
            "total": total, "on_credit": principal or ZERO,
            "title": r["customer__name"] or r["customer_name_snapshot"] or "",
            "reference": r["reference"] or "",
            "method": r["payment_method"] or "",
            "method_display": sale_methods.get(r["payment_method"], ""),
            "by": names.get(r["sold_by_id"], ""),
            "target": r["id"],
        })
    for r in repayments:
        moment = _moment(r["paid_at"])
        items.append({
            "id": r["id"], "moment": moment, "day": moment.date(), "has_time": True,
            "kind": "repaid", "type": "repayment", "amount": r["amount"] or ZERO,
            "total": None, "on_credit": None,
            "title": r["debt__customer__name"] or "",
            "reference": r["debt__reference"] or r["reference"] or "",
            "method": r["method"] or "",
            "method_display": repay_methods.get(r["method"], ""),
            "by": names.get(r["received_by_id"], ""),
            "target": r["debt_id"],
        })
    items.sort(key=lambda i: (i["moment"], i["type"], i["id"]))
    return items


def money_in(user, period: Period, with_events=False):
    """
    Cash that actually arrived: what was paid at the till, plus debts paid
    off during the period - each counted on the day the money came.
    """
    items = _in_items(user, period)
    at_till = sum((i["amount"] for i in items if i["kind"] == "till"), ZERO)
    repaid = sum((i["amount"] for i in items if i["kind"] == "repaid"), ZERO)
    result = {
        "total": money(at_till + repaid),
        "at_till": money(at_till),
        "repaid": money(repaid),
    }
    if not with_events:
        return result
    return result, [(i["moment"], i["amount"]) for i in items if i["amount"]]


# ---------------------------------------------------------------------------
# Profit
# ---------------------------------------------------------------------------
def profit(user, period: Period) -> dict:
    """Profit after all costs - the figure that needs no guessing."""
    from expenses.services import running_costs, summarize
    from expenses.models import Expense

    from .selectors import cost_of_goods_sold, sales_summary

    sales = sales_summary(period.start, period.end, user=user)
    cogs = cost_of_goods_sold(period.start, period.end, user=user)
    summary = summarize(scoped(Expense.objects.between(period.start, period.end), user))
    running = running_costs(summary)
    value = money(sales["revenue"] - cogs - running)
    return {
        "revenue": sales["revenue"],
        "sales_count": sales["count"],
        "outstanding": sales["outstanding"],
        "cost_of_sold": cogs,
        "running_costs": money(running),
        "gross": money(sales["revenue"] - cogs),
        "profit": value,
        "margin": _pct(value, sales["revenue"]),
    }


def _pct(part, whole):
    if not whole:
        return None
    return (Decimal(part) / Decimal(whole) * 100).quantize(Decimal("0.1"), ROUND_HALF_UP)


def _change(now, before):
    """Percentage change, or None when there is nothing to compare with."""
    if not before:
        return None
    return ((Decimal(now) - Decimal(before)) / abs(Decimal(before)) * 100).quantize(
        Decimal("0.1"), ROUND_HALF_UP
    )


# ---------------------------------------------------------------------------
# What is on hand
# ---------------------------------------------------------------------------
def holdings(user) -> dict:
    """Raw materials and finished goods on hand now, and what is owed."""
    from credit.models import DebtRecord
    from inventory.models import Product
    from production.models import RawMaterial

    materials = []
    material_value = ZERO
    # Every material that is not deleted - switched off ones too, as on the
    # Materials page: a material nobody orders any more is still worth what
    # is left of it.
    for m in scoped(RawMaterial.objects.alive(), user).order_by("name"):
        if not m.quantity_in_stock:
            continue
        value = money(m.quantity_in_stock * (m.unit_cost or ZERO))
        material_value += value
        materials.append({
            "id": m.pk, "name": m.name, "quantity": m.quantity_in_stock,
            "unit_display": m.get_unit_display(), "unit_cost": m.unit_cost, "value": value,
            "status": m.stock_status, "reorder_level": m.reorder_level,
        })
    materials.sort(key=lambda r: -r["value"])

    products = []
    at_cost = at_price = ZERO
    for p in scoped(Product.objects.alive().filter(is_active=True), user).order_by("name"):
        if p.stock_quantity <= 0:
            continue
        cost = p.audit_unit_cost
        value = money(cost * p.stock_quantity)
        retail = money(p.selling_price * p.stock_quantity)
        at_cost += value
        at_price += retail
        products.append({
            "id": p.pk, "name": p.name, "quantity": p.stock_quantity,
            "unit_display": p.get_unit_display(), "unit_cost": cost,
            "your_cost_set": p.audit_cost is not None,
            "batch_cost": p.cost_price, "selling_price": p.selling_price,
            "value": value, "retail_value": retail,
            "status": p.stock_status,
        })
    products.sort(key=lambda r: -r["value"])

    owed = money(
        _sum(scoped(DebtRecord.objects.all(), user).open_debts(), F("balance"))
    )
    total = money(material_value + at_cost + owed)
    return {
        "materials": materials,
        "materials_value": money(material_value),
        "products": products,
        "products_value": money(at_cost),
        "products_retail": money(at_price),
        "products_potential": money(at_price - at_cost),
        "owed": owed,
        "total": total,
        "kinds": [
            {"key": "materials", "amount": money(material_value)},
            {"key": "products", "amount": money(at_cost)},
            {"key": "owed", "amount": owed},
        ],
    }


# ---------------------------------------------------------------------------
# Cost per unit
# ---------------------------------------------------------------------------
def costing(user, period: Period, running_pool: Decimal) -> list[dict]:
    """
    One row per product that did anything: was made, was sold, is on the
    shelf, or has a cost from the owner.

        suggested = what one unit measurably costs + its share of the
                    period's running costs

    "Measurably costs" is materials plus batch costs per unit, from the
    batches made in the period - or, for a product not made in it (or bought
    in rather than made), what it last cost (cost_price).

    THE SHARE
    ---------
    Running costs - wages, electricity, transport, rent - are split between
    products by how much work each was, and spread over the units that work
    produced or moved:

        work   = units x measurable cost per unit
        units  = the larger of units made and units sold

    Weighting by cost means a big block that takes three times the cement
    carries three times the share of a small one. Taking the larger of made
    and sold keeps a slow production week from loading a whole month's wages
    onto sixty blocks - which is what a split over units made alone did, and
    suggested a block costs 146 when it sells for 32.
    """
    from inventory.models import Product
    from production.models import ProductionRun
    from sales.models import TransactionItem

    made = defaultdict(lambda: {"units": 0, "materials": ZERO, "extras": ZERO,
                                "batches": 0, "costs": []})
    for product_id, produced_on, units, mat, other, unit_cost in scoped(
        ProductionRun.objects.completed().filter(
            produced_on__gte=period.start, produced_on__lte=period.end
        ),
        user,
    ).order_by("produced_on", "id").values_list(
        "product_id", "produced_on", "quantity_produced", "material_cost",
        "other_cost", "unit_cost",
    ):
        row = made[product_id]
        row["units"] += units or 0
        row["materials"] += mat or ZERO
        row["extras"] += other or ZERO
        row["batches"] += 1
        if units:
            row["costs"].append((produced_on, unit_cost or ZERO))

    sold = {
        r["product_id"]: r
        for r in scoped(TransactionItem.objects.all(), user)
        .filter(
            transaction__is_voided=False,
            transaction__created_at__date__gte=period.start,
            transaction__created_at__date__lte=period.end,
        )
        .values("product_id")
        .annotate(
            units=Coalesce(Sum("quantity"), 0),
            revenue=Coalesce(Sum("line_total", output_field=DEC), ZERO, output_field=DEC),
            cost=Coalesce(
                Sum(ExpressionWrapper(F("unit_cost") * F("quantity"), output_field=DEC)),
                ZERO, output_field=DEC,
            ),
        )
    }

    ids = set(made) | set(sold)
    products = scoped(Product.objects.alive(), user).select_related("audit_cost_set_by")
    rows = []
    for p in products.order_by("name"):
        active = p.pk in ids or p.stock_quantity > 0 or p.audit_cost is not None
        if not active or (not p.is_active and p.pk not in ids):
            continue
        m = made.get(p.pk)
        s = sold.get(p.pk)
        row = {
            "id": p.pk, "name": p.name, "sku": p.sku, "unit_display": p.get_unit_display(),
            "selling_price": p.selling_price,
            "system_cost": p.cost_price,
            "your_cost": p.audit_cost,
            "your_cost_note": p.audit_cost_note,
            "your_cost_set_at": p.audit_cost_set_at,
            "your_cost_set_by": (
                p.audit_cost_set_by.display_name if p.audit_cost_set_by_id else ""
            ),
            "in_stock": p.stock_quantity,
            "produced": 0, "batches": 0,
            "materials_per_unit": None, "extras_per_unit": None,
            "base_per_unit": p.cost_price, "base_source": "system",
            "running_per_unit": None, "suggested": None,
            "batch_low": None, "batch_high": None, "batch_last": None,
            "batch_trend": None,
            "sold": 0, "revenue": ZERO, "recorded_cost": ZERO,
            "profit_recorded": ZERO, "profit_at_your_cost": None,
        }
        if m and m["units"]:
            units = Decimal(m["units"])
            materials_pu = m["materials"] / units
            extras_pu = m["extras"] / units
            costs = [c for _, c in m["costs"]]
            row.update({
                "produced": m["units"], "batches": m["batches"],
                "materials_per_unit": money(materials_pu),
                "extras_per_unit": money(extras_pu),
                "base_per_unit": money(materials_pu + extras_pu),
                "base_source": "batches",
                "batch_low": money(min(costs)) if costs else None,
                "batch_high": money(max(costs)) if costs else None,
                "batch_last": money(costs[-1]) if costs else None,
                "batch_trend": _change(costs[-1], costs[0]) if len(costs) > 1 else None,
            })
        if s:
            row.update({
                "sold": s["units"],
                "revenue": money(s["revenue"]),
                "recorded_cost": money(s["cost"]),
                "profit_recorded": money(s["revenue"] - s["cost"]),
            })
            if p.audit_cost is not None:
                row["profit_at_your_cost"] = money(
                    s["revenue"] - p.audit_cost * s["units"]
                )
        row["work_units"] = max(row["produced"], row["sold"])
        rows.append(row)

    # Share the running costs out by work done (see the docstring).
    weights = {r["id"]: Decimal(r["work_units"]) * (r["base_per_unit"] or ZERO) for r in rows}
    total_weight = sum(weights.values(), ZERO)
    total_units = sum((r["work_units"] for r in rows), 0)
    for r in rows:
        if not r["work_units"]:
            continue
        if total_weight > 0:
            share = weights[r["id"]] / total_weight
        else:
            share = Decimal(r["work_units"]) / Decimal(total_units)
        running_pu = running_pool * share / Decimal(r["work_units"]) if running_pool > 0 else ZERO
        r["running_per_unit"] = money(running_pu)
        r["suggested"] = money((r["base_per_unit"] or ZERO) + running_pu)

    for r in rows:
        # What the price is held against: the owner's figure, else the
        # suggestion, else what it last cost.
        if r["your_cost"] is not None:
            cost, basis = r["your_cost"], "yours"
        elif r["suggested"] is not None:
            cost, basis = r["suggested"], "suggested"
        elif r["system_cost"]:
            cost, basis = r["system_cost"], "system"
        else:
            cost, basis = None, ""
        price = r["selling_price"]
        r["cost_basis"] = basis
        r["cost_used"] = cost
        r["margin_at_your_cost"] = (
            _pct(price - r["your_cost"], price) if r["your_cost"] is not None else None
        )
        r["margin_at_suggested"] = (
            _pct(price - r["suggested"], price) if r["suggested"] is not None else None
        )
        r["below_cost"] = bool(cost is not None and price < cost)

    rows.sort(key=lambda r: (-(r["revenue"] or 0), -r["produced"], r["name"]))
    return rows


def cost_candles(user, period: Period, product_id) -> list[dict]:
    """Batch cost per unit for one product, as candles."""
    from production.models import ProductionRun

    points = list(
        scoped(ProductionRun.objects.completed(), user)
        .filter(
            product_id=product_id,
            produced_on__gte=period.start, produced_on__lte=period.end,
            quantity_produced__gt=0,
        )
        .order_by("produced_on", "id")
        .values_list("produced_on", "unit_cost")
    )
    return value_candles(points, period)


# ---------------------------------------------------------------------------
# Insights
# ---------------------------------------------------------------------------
def _insights(report: dict) -> list[dict]:
    """
    The analysis, as short findings: what is wrong first, then what is good.

    Each is {level, code, params}. The words live with the clients - the web
    template and the phone's two languages - so a finding reads naturally in
    Amharic instead of being a translated English sentence with numbers in it.
    """
    out = []
    add = lambda level, code, **params: out.append(  # noqa: E731
        {"level": level, "code": code, "params": params}
    )
    out_now, came_in = report["money_out"], report["money_in"]
    pr, prev = report["profit"], report.get("previous") or {}
    rows = report["costing"]

    if not (out_now["total"] or came_in["total"] or pr["revenue"] or rows):
        add("info", "no_activity")
        return out

    # -- What the products themselves say ----------------------------------
    for r in rows:
        if r["below_cost"]:
            add("critical", "below_cost", product=r["name"], product_id=r["id"],
                price=r["selling_price"], cost=r["cost_used"], basis=r["cost_basis"])
    for r in rows:
        if r["your_cost"] is None or r["suggested"] is None or not r["suggested"]:
            continue
        gap = (r["your_cost"] - r["suggested"]) / r["suggested"]
        if gap < -ESTIMATE_TOLERANCE:
            add("warning", "estimate_low", product=r["name"], product_id=r["id"],
                cost=r["your_cost"], suggested=r["suggested"],
                percent=(abs(gap) * 100).quantize(Decimal("1"), ROUND_HALF_UP))
    missing = [r for r in rows if r["your_cost"] is None and (r["sold"] or r["produced"])]
    if missing:
        add("warning", "costs_missing", count=len(missing),
            products=", ".join(r["name"] for r in missing[:3]))

    # -- Do the owner's costs explain the real profit? ---------------------
    sold_rows = [r for r in rows if r["sold"]]
    revenue_rows = sum((r["revenue"] for r in sold_rows), ZERO)
    covered = sum((r["revenue"] for r in sold_rows if r["your_cost"] is not None), ZERO)
    if revenue_rows > 0 and covered / revenue_rows >= Decimal("0.8"):
        at_yours = sum(
            (r["profit_at_your_cost"] for r in sold_rows if r["profit_at_your_cost"] is not None),
            ZERO,
        )
        books = pr["profit"]
        scale = max(abs(books), pr["revenue"] * Decimal("0.05"), Decimal("1"))
        diff = (at_yours - books) / scale
        if diff > ESTIMATE_TOLERANCE:
            add("warning", "estimates_too_low", at_yours=money(at_yours), books=books)
        elif diff < -ESTIMATE_TOLERANCE:
            add("info", "estimates_too_high", at_yours=money(at_yours), books=books)
        else:
            add("good", "estimates_right", at_yours=money(at_yours), books=books)

    # -- Batch cost moving --------------------------------------------------
    for r in rows:
        trend = r["batch_trend"]
        if trend is None or r["batches"] < 2:
            continue
        if trend >= 8:
            add("warning", "cost_rising", product=r["name"], product_id=r["id"],
                percent=trend, first=r["batch_low"], last=r["batch_last"])
        elif trend <= -8:
            add("good", "cost_falling", product=r["name"], product_id=r["id"],
                percent=abs(trend))

    # -- The whole business ------------------------------------------------
    if pr["revenue"] and pr["profit"] < 0:
        add("critical", "loss", amount=abs(pr["profit"]))
    elif pr["revenue"] and pr["profit"] > 0:
        add("good", "profit", amount=pr["profit"], margin=pr["margin"])

    if prev.get("money_out") and out_now["total"]:
        change = _change(out_now["total"], prev["money_out"])
        if change is not None and change >= 15:
            add("warning", "spend_up", percent=change)
        elif change is not None and change <= -10:
            add("good", "spend_down", percent=abs(change))

    owed = report["holdings"]["owed"]
    if pr["revenue"] and owed > pr["revenue"] * Decimal("0.3"):
        add("warning", "owed_high", amount=owed, percent=_pct(owed, pr["revenue"]))

    slow = [p for p in report["holdings"]["products"]
            if not any(r["id"] == p["id"] and r["sold"] for r in rows)]
    if slow:
        add("warning", "slow_stock", count=len(slow),
            value=money(sum((p["value"] for p in slow), ZERO)),
            products=", ".join(p["name"] for p in slow[:3]))

    used = report["production"]["materials_used"]
    on_hand = report["holdings"]["materials_value"]
    if used > 0 and on_hand > 0:
        days = int(on_hand / (used / report["period"]["days"]))
        level = "warning" if days > 90 or days < 7 else "info"
        add(level, "materials_cover", days=days, value=on_hand)

    net = came_in["total"] - out_now["total"]
    if net < 0:
        add("info", "cash_out", amount=money(-net))
    elif net > 0:
        add("good", "cash_in", amount=money(net))

    out.sort(key=lambda i: LEVEL_ORDER[i["level"]])
    return out[:MAX_INSIGHTS]


# ---------------------------------------------------------------------------
# The report
# ---------------------------------------------------------------------------
def production_summary(user, period: Period) -> dict:
    from production.models import MaterialMovement, MaterialMovementType, ProductionRun

    runs = scoped(ProductionRun.objects.completed(), user).filter(
        produced_on__gte=period.start, produced_on__lte=period.end
    )
    agg = runs.aggregate(
        batches=Count("id"),
        units=Coalesce(Sum("quantity_produced"), 0),
        rejected=Coalesce(Sum("quantity_rejected"), 0),
        materials=Coalesce(Sum("material_cost", output_field=DEC), ZERO, output_field=DEC),
        extras=Coalesce(Sum("other_cost", output_field=DEC), ZERO, output_field=DEC),
    )
    # Used, less whatever a reversed batch put back.
    used = -_sum(
        scoped(MaterialMovement.objects.all(), user).filter(
            movement_type__in=[
                MaterialMovementType.CONSUMED,
                MaterialMovementType.PRODUCTION_REVERSAL,
            ],
            created_at__date__gte=period.start, created_at__date__lte=period.end,
        ),
        ExpressionWrapper(F("quantity_delta") * F("unit_cost"), output_field=DEC),
    )
    return {
        "batches": agg["batches"],
        "units": agg["units"],
        "rejected": agg["rejected"],
        "materials": money(agg["materials"]),
        "extras": money(agg["extras"]),
        "materials_used": money(max(used, ZERO)),
    }


def build_report(user, period: Period, product_id=None) -> dict:
    """Everything the Audit page shows, for one person and one period."""
    spent, out_events = money_out(user, period, with_events=True)
    came, in_events = money_in(user, period, with_events=True)
    pr = profit(user, period)
    rows = costing(user, period, Decimal(pr["running_costs"]))

    previous = None
    if period.key != "all":
        before = period.previous()
        prev_profit = profit(user, before)
        previous = {
            "start": before.start, "end": before.end,
            "money_out": money_out(user, before)["total"],
            "money_in": money_in(user, before)["total"],
            "revenue": prev_profit["revenue"],
            "profit": prev_profit["profit"],
        }

    # The product the cost candles follow: the one asked for, else the one
    # made most of in the period, else the best seller.
    known = {r["id"] for r in rows}
    chosen = None
    try:
        asked = int(product_id) if product_id not in (None, "") else None
    except (TypeError, ValueError):
        asked = None
    if asked in known:
        chosen = asked
    elif rows:
        made = sorted((r for r in rows if r["produced"]), key=lambda r: -r["produced"])
        chosen = (made[0] if made else rows[0])["id"]

    report = {
        "period": {
            "start": period.start, "end": period.end, "key": period.key,
            "days": period.days, "bucket": period.bucket,
        },
        "previous": previous,
        "money_out": spent,
        "money_in": came,
        "profit": pr,
        "holdings": holdings(user),
        "production": production_summary(user, period),
        "costing": rows,
        "cash_candles": running_candles(out_events + in_events, period),
        "cost_product_id": chosen,
        "cost_candles": cost_candles(user, period, chosen) if chosen else [],
        "can_set_cost": bool(user.has_access("costing.set")),
    }
    if previous:
        report["changes"] = {
            "money_out": _change(spent["total"], previous["money_out"]),
            "money_in": _change(came["total"], previous["money_in"]),
            "revenue": _change(pr["revenue"], previous["revenue"]),
            "profit": _change(pr["profit"], previous["profit"]),
        }
    else:
        report["changes"] = {"money_out": None, "money_in": None, "revenue": None, "profit": None}
    report["insights"] = _insights(report)
    return report


# ---------------------------------------------------------------------------
# Setting the owner's figure
# ---------------------------------------------------------------------------
MAX_COST = Decimal("9999999999.99")


class CostError(ValueError):
    """A cost that cannot be stored. The message is safe to show."""


def set_your_cost(product, cost, *, user, note="", request=None):
    """
    The owner says what one unit of `product` really costs. None clears it.

    Stored on the product and appended to its history, and written to the
    audit log - a figure every margin in the Audit is worked out from is
    worth knowing who changed, and when.
    """
    from django.db import transaction

    from accounts.models import AuditAction
    from accounts.services import log_action
    from inventory.models import Product, ProductCostEstimate

    if cost not in (None, ""):
        try:
            cost = money(Decimal(str(cost).replace(",", "").strip()))
        except Exception:
            raise CostError("Enter the cost as a number, for example 24.50.")
        if cost < 0:
            raise CostError("A cost cannot be below zero.")
        if cost > MAX_COST:
            raise CostError("That cost is too large.")
    else:
        cost = None
    note = (note or "").strip()[:255]

    with transaction.atomic():
        locked = Product.objects.select_for_update().get(pk=product.pk)
        previous = locked.audit_cost
        locked.audit_cost = cost
        locked.audit_cost_note = note if cost is not None else ""
        locked.audit_cost_set_at = timezone.now() if cost is not None else None
        locked.audit_cost_set_by = user if cost is not None else None
        locked.save(update_fields=[
            "audit_cost", "audit_cost_note", "audit_cost_set_at",
            "audit_cost_set_by", "updated_at",
        ])
        ProductCostEstimate.objects.create(
            product=locked, cost=cost, previous=previous,
            system_cost=locked.cost_price, note=note, set_by=user,
        )
        if cost is None:
            text = f"Cleared the owner's cost of {locked.name} (was {previous})."
        elif previous is None:
            text = f"Set the cost of one {locked.name} to {cost}."
        else:
            text = f"Changed the cost of one {locked.name} from {previous} to {cost}."
        log_action(
            AuditAction.UPDATE, instance=locked, description=text,
            changes={"audit_cost": {"from": str(previous) if previous is not None else "",
                                    "to": str(cost) if cost is not None else ""}},
            user=user, request=request,
        )
    product.audit_cost = locked.audit_cost
    product.audit_cost_note = locked.audit_cost_note
    product.audit_cost_set_at = locked.audit_cost_set_at
    product.audit_cost_set_by = locked.audit_cost_set_by
    return locked
