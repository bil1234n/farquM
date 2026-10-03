"""
The Audit's four cards, opened up: every figure behind each one, drawn and
listed, so the owner can check a card against his own notebook line by line.

    money-out   what left the business - by kind, by day, by material and by
                expense category - and every payment, newest first
    money-in    what came back - at the till and as debts paid off - by day
                and by who took it, and every payment
    profit      sales, less the cost of what was sold, less running costs -
                by day and by product
    on-hand     what is on the shelves and owed, item by item

A page's total is ALWAYS its card's total. Money out and money in are added
up from the same item lists the cards use (reports/audit.py: _out_items,
_in_items); profit and what is on hand come from the very same functions.
Nothing here works a figure out a second way, so the two cannot drift apart.

The words are the clients' own, as on the Audit page: rows carry codes
("delivery", "wage", "till") and the web template and the phone's two
languages say them.
"""
from collections import defaultdict
from decimal import Decimal

from django.utils import timezone

from core.scoping import scoped
from core.utils import ZERO, money

from .audit import (
    SPEND_KINDS,
    Period,
    _change,
    _in_items,
    _out_items,
    _out_totals,
    _pct,
    bucket_of,
    buckets,
    costing,
    holdings,
    money_in,
    money_out,
    profit,
)

#: The four pages, as they appear in addresses: /reports/audit/money-out/.
KINDS = ("money-out", "money-in", "profit", "on-hand")

#: Lines listed on a page at most. The totals always cover everything; the
#: page says how many lines there were in all.
MAX_ITEMS = 300

#: The two halves of money in, in their drawing (and colour) order.
IN_KINDS = ("till", "repaid")

#: What the profit bars are made of, per bucket.
PROFIT_PARTS = ("revenue", "cost", "running")


def _period(period: Period) -> dict:
    return {
        "start": period.start, "end": period.end, "key": period.key,
        "days": period.days, "bucket": period.bucket,
    }


def series(entries, period: Period, keys) -> list[dict]:
    """
    Per bucket - a day, a week or a month, like the candles - the amount for
    each key, in the order of `keys` (which is also the colour order).

    `entries` are (day, key, amount). The first bucket starts on the
    period's first day, as the candles' does, so bars and candles line up.
    """
    keys = list(keys)
    rows = [{"start": s, "end": e, "values": [ZERO] * len(keys)} for s, e in buckets(period)]
    at = {r["start"]: r for r in rows}
    slot = {k: n for n, k in enumerate(keys)}
    for day, key, amount in entries:
        row = at.get(max(bucket_of(day, period.bucket), period.start))
        if row is None or key not in slot:
            continue
        row["values"][slot[key]] += amount or ZERO
    for r in rows:
        r["values"] = [money(v) for v in r["values"]]
        r["total"] = money(sum(r["values"], ZERO))
    return rows


#: Extra facts an item may carry, copied onto its row when present.
_ROW_EXTRAS = (
    "party", "note", "in_batch", "target", "quantity", "unit_display", "deleted",
    "method", "method_display",
)
_ROW_MONEY = ("unit_cost", "total", "on_credit")


def _row(item) -> dict:
    """One line of a page, as the clients list it."""
    row = {
        "id": item["id"],
        "type": item["type"],
        "kind": item["kind"],
        "at": item["moment"],
        "has_time": item["has_time"],
        "amount": money(item["amount"]),
        "title": item["title"],
        "reference": item["reference"],
        "by": item["by"],
    }
    for key in _ROW_EXTRAS:
        if key in item:
            row[key] = item[key]
    for key in _ROW_MONEY:
        if key in item:
            row[key] = money(item[key]) if item[key] is not None else None
    return row


# ---------------------------------------------------------------------------
# Money out
# ---------------------------------------------------------------------------
def money_out_detail(user, period: Period) -> dict:
    items, not_counted = _out_items(user, period)
    totals = _out_totals(items, not_counted)
    before = None if period.key == "all" else money_out(user, period.previous())["total"]

    def grouped(types):
        rows = {}
        for i in items:
            if i["type"] not in types:
                continue
            r = rows.setdefault(i["target"], {
                "id": i["target"], "name": i["title"], "unit_display": i["unit_display"],
                "quantity": ZERO, "amount": ZERO, "count": 0, "deleted": i["deleted"],
            })
            r["quantity"] += i["quantity"] or ZERO
            r["amount"] += i["amount"]
            r["count"] += 1
        out = sorted(rows.values(), key=lambda r: (-r["amount"], r["name"]))
        for r in out:
            r["amount"] = money(r["amount"])
        return out

    newest = items[::-1]
    return {
        "kind": "money_out",
        "period": _period(period),
        "total": totals["total"],
        "change": _change(totals["total"], before) if before is not None else None,
        "figures": {
            key: totals[key]
            for key in ("expenses", "wages", "running", "in_batches", "materials",
                        "stock_bought", "opening", "bought_in", "not_counted")
        },
        "series_keys": list(SPEND_KINDS),
        "series": series(((i["day"], i["kind"], i["amount"]) for i in items), period, SPEND_KINDS),
        "parts": totals["kinds"],
        "categories": totals["categories"],
        "by_material": grouped({"delivery", "returned", "opening"}),
        "by_product": grouped({"stock", "stock_returned", "stock_opening"}),
        "items": [_row(i) for i in newest[:MAX_ITEMS]],
        "items_count": len(items),
        "not_counted": {
            "amount": totals["not_counted"],
            "count": len(not_counted),
            "items": [_row(i) for i in not_counted[::-1][:MAX_ITEMS]],
        },
    }


# ---------------------------------------------------------------------------
# Money in
# ---------------------------------------------------------------------------
def money_in_detail(user, period: Period) -> dict:
    items = _in_items(user, period)
    paid = [i for i in items if i["amount"]]
    at_till = sum((i["amount"] for i in paid if i["kind"] == "till"), ZERO)
    repaid = sum((i["amount"] for i in paid if i["kind"] == "repaid"), ZERO)
    total = money(at_till + repaid)
    before = None if period.key == "all" else money_in(user, period.previous())["total"]

    people = defaultdict(lambda: {"amount": ZERO, "count": 0})
    for i in paid:
        person = people[i["by"] or "-"]
        person["amount"] += i["amount"]
        person["count"] += 1
    by_person = sorted(
        ({"name": name, "amount": money(p["amount"]), "count": p["count"]}
         for name, p in people.items()),
        key=lambda r: (-r["amount"], r["name"]),
    )
    sales = [i for i in items if i["type"] == "sale"]
    return {
        "kind": "money_in",
        "period": _period(period),
        "total": total,
        "change": _change(total, before) if before is not None else None,
        "figures": {
            "at_till": money(at_till),
            "repaid": money(repaid),
            "sales": len(sales),
            "repayments": sum(1 for i in paid if i["kind"] == "repaid"),
            # Sold in the period but not paid for yet - not money in.
            "on_credit": money(sum((i["on_credit"] or ZERO for i in sales), ZERO)),
        },
        "series_keys": list(IN_KINDS),
        "series": series(((i["day"], i["kind"], i["amount"]) for i in paid), period, IN_KINDS),
        "parts": [
            {"key": "till", "amount": money(at_till)},
            {"key": "repaid", "amount": money(repaid)},
        ],
        "by_person": by_person,
        "items": [_row(i) for i in paid[::-1][:MAX_ITEMS]],
        "items_count": len(paid),
    }


# ---------------------------------------------------------------------------
# Profit
# ---------------------------------------------------------------------------
def profit_detail(user, period: Period) -> dict:
    """
    Profit after all costs, opened up. Every bucket is worked out the way the
    card is - sales, less the cost copied onto each sale line, less running
    costs (expenses not paid against a batch) - so the bars add up to it.
    """
    from expenses.models import Expense
    from sales.models import Transaction, TransactionItem

    start, end = period.start, period.end
    pr = profit(user, period)
    before = None if period.key == "all" else profit(user, period.previous())["profit"]

    def local_day(value):
        return timezone.localtime(value).date() if timezone.is_aware(value) else value.date()

    entries = []
    for created, total in scoped(
        Transaction.objects.active().filter(
            created_at__date__gte=start, created_at__date__lte=end
        ),
        user,
    ).values_list("created_at", "total_amount"):
        entries.append((local_day(created), "revenue", total or ZERO))
    for created, unit_cost, quantity in scoped(
        TransactionItem.objects.filter(
            transaction__is_voided=False,
            transaction__created_at__date__gte=start,
            transaction__created_at__date__lte=end,
        ),
        user,
    ).values_list("transaction__created_at", "unit_cost", "quantity"):
        entries.append((local_day(created), "cost", (unit_cost or ZERO) * (quantity or 0)))
    running_by_category = defaultdict(lambda: [ZERO, 0])
    for spent_on, amount, category in scoped(
        Expense.objects.between(start, end).filter(production_run__isnull=True), user
    ).values_list("spent_on", "amount", "category_name"):
        entries.append((spent_on, "running", amount or ZERO))
        row = running_by_category[category or "-"]
        row[0] += amount or ZERO
        row[1] += 1

    rows = []
    for r in series(entries, period, PROFIT_PARTS):
        revenue, cost, running = r["values"]
        value = money(revenue - cost - running)
        rows.append({
            "start": r["start"], "end": r["end"], "values": [value], "total": value,
            "revenue": revenue, "cost": cost, "running": running,
        })

    products = []
    covered = at_yours = ZERO
    for r in costing(user, period, Decimal(pr["running_costs"])):
        if not r["sold"]:
            continue
        products.append({
            "id": r["id"], "name": r["name"], "unit_display": r["unit_display"],
            "sold": r["sold"], "revenue": r["revenue"],
            "recorded_cost": r["recorded_cost"], "profit_recorded": r["profit_recorded"],
            "margin_recorded": _pct(r["profit_recorded"], r["revenue"]),
            "your_cost": r["your_cost"],
            "profit_at_your_cost": r["profit_at_your_cost"],
            "margin_at_your_cost": (
                _pct(r["profit_at_your_cost"], r["revenue"])
                if r["profit_at_your_cost"] is not None else None
            ),
        })
        if r["profit_at_your_cost"] is not None:
            covered += r["revenue"]
            at_yours += r["profit_at_your_cost"]

    categories = sorted(
        ({"label": label, "total": money(t), "count": n}
         for label, (t, n) in running_by_category.items()),
        key=lambda r: (-r["total"], r["label"]),
    )
    return {
        "kind": "profit",
        "period": _period(period),
        "total": pr["profit"],
        "change": _change(pr["profit"], before) if before is not None else None,
        "figures": {
            "revenue": pr["revenue"],
            "cost_of_sold": pr["cost_of_sold"],
            "gross": pr["gross"],
            "running_costs": pr["running_costs"],
            "profit": pr["profit"],
            "margin": pr["margin"],
            "sales_count": pr["sales_count"],
            "outstanding": pr["outstanding"],
            # What the owner's own costs say the products made, and how much
            # of the sales those costs cover - see the Audit's two profits.
            "at_your_costs": money(at_yours) if covered else None,
            "covered": _pct(covered, pr["revenue"]),
        },
        "series_keys": ["profit"],
        "series": rows,
        "steps": [
            {"key": "revenue", "amount": pr["revenue"]},
            {"key": "cost_of_sold", "amount": -pr["cost_of_sold"]},
            {"key": "running", "amount": -pr["running_costs"]},
            {"key": "profit", "amount": pr["profit"]},
        ],
        "products": products,
        "running_categories": categories,
    }


# ---------------------------------------------------------------------------
# On hand
# ---------------------------------------------------------------------------
def on_hand_detail(user, period: Period) -> dict:
    """What the business holds right now. The period plays no part."""
    from credit.models import DebtRecord

    held = holdings(user)
    today = timezone.localdate()
    debts = []
    for pk, reference, customer, balance, due, issued in (
        scoped(DebtRecord.objects.all(), user).open_debts()
        .order_by("due_date", "id")
        .values_list("pk", "reference", "customer__name", "balance", "due_date", "issued_date")
    ):
        debts.append({
            "id": pk, "reference": reference, "customer": customer or "",
            "balance": money(balance or ZERO), "due_date": due, "issued_date": issued,
            "days_overdue": (today - due).days if due and due < today else 0,
        })
    return {
        "kind": "on_hand",
        "period": _period(period),
        "as_of": today,
        "total": held["total"],
        "change": None,
        "figures": {
            key: held[key]
            for key in ("materials_value", "products_value", "products_retail",
                        "products_potential", "owed")
        },
        "parts": held["kinds"],
        "materials": held["materials"],
        "products": held["products"],
        "debts": debts[:MAX_ITEMS],
        "debts_count": len(debts),
    }


BUILDERS = {
    "money-out": money_out_detail,
    "money-in": money_in_detail,
    "profit": profit_detail,
    "on-hand": on_hand_detail,
}


def build_detail(user, period: Period, kind: str) -> dict:
    """One card's page. `kind` is one of KINDS; anything else is a KeyError."""
    return BUILDERS[kind](user, period)
