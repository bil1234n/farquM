"""
The endpoints that close the gap between the web app and the phone.

WHY A SECOND VIEWS MODULE
-------------------------
`api/views.py` is the CRUD surface - one viewset per model, each a thin wrapper
over the same services the web UI calls. What is here is different in kind:
read-only aggregates, a CSV writer, an audit trail, and the registration
passcode screen. Filing them alongside the viewsets would have made a long file
longer without making either half easier to find.

THE RULE THEY ALL FOLLOW
------------------------
Nothing here computes a figure of its own. Every number comes from
`reports.selectors` or `credit.services`, which is what the web pages use, so
the phone and the browser cannot disagree about this month's revenue. A second
implementation of "revenue" is how two screens end up showing different totals
and nobody can say which is right.

Scoping is not optional either: every queryset goes through `core.scoping`, so
a sales assistant opening the receivables report sees their own book and a
manager sees their team's.
"""
import csv
import datetime as dt

from django.db.models import Q
from django.http import HttpResponse
from django.utils import timezone
from rest_framework.decorators import api_view, permission_classes
from rest_framework.response import Response

from accounts.models import AuditAction, AuditLog, RegistrationPasscode, RoleDefinition
from accounts.registration import ensure_passcode_rows, has_server_passcode, registration_status
from accounts.services import log_action
from core.models import SystemSetting
from core.scoping import scoped, sees_everything
from credit.models import CreditAccount, DebtRecord
from inventory.models import Product, StockMovement
from reports.selectors import (
    collections_summary,
    daily_series,
    inventory_valuation,
    profit_summary,
    receivables_summary,
    sales_by_staff,
    sales_summary,
    top_products,
)
from sales.models import Customer, Transaction

from .permissions import requires
from .serializers import DebtSerializer, StockMovementSerializer

#: A report asked for with no dates covers the last month. Long enough to be
#: useful on a first open, short enough not to scan a year of rows on a phone.
DEFAULT_WINDOW_DAYS = 30

#: The most rows any one report will send to a phone. A shop with 40,000 sales
#: must not turn one tap into a 12 MB download on mobile data; the report
#: screens page or link to the CSV for the full set.
ROW_CAP = 200


def _period(request, default_days: int = DEFAULT_WINDOW_DAYS):
    """
    Parse ?date_from= / ?date_to= into a validated (start, end).

    Anything unparseable falls back to the default window rather than 400ing:
    a report is a read, and showing the last month beats showing an error
    because a date arrived in the wrong format.
    """
    today = timezone.localdate()

    def parse(raw, fallback):
        try:
            return dt.date.fromisoformat((raw or "").strip())
        except (TypeError, ValueError):
            return fallback

    start = parse(request.query_params.get("date_from"),
                  today - dt.timedelta(days=default_days))
    end = parse(request.query_params.get("date_to"), today)
    if start > end:
        start, end = end, start
    return start, end


def _money_map(mapping):
    return {key: str(value) for key, value in mapping.items()}


# ---------------------------------------------------------------------------
# Reports
# ---------------------------------------------------------------------------
@api_view(["GET"])
@permission_classes([requires("report.sales")])
def sales_report(request):
    """Sales over a period, with the per-person table when it means anything."""
    start, end = _period(request)
    user = request.user

    transactions = (
        scoped(Transaction.objects.active(), user)
        .filter(created_at__date__gte=start, created_at__date__lte=end)
        .select_related("customer", "sold_by")
        .order_by("-created_at")
    )

    payload = {
        "start": start.isoformat(),
        "end": end.isoformat(),
        "summary": _money_map(
            {
                k: v
                for k, v in sales_summary(start, end, user=user).items()
                if k != "count"
            }
        )
        | {"count": sales_summary(start, end, user=user)["count"]},
        "chart": [
            {
                "date": row["date"].isoformat(),
                "label": row["label"],
                "revenue": str(row["revenue"]),
                "count": row["count"],
            }
            for row in daily_series(start, end, user=user)
        ],
        "transaction_count": transactions.count(),
        "top_products": [
            {
                "name": row["name"],
                "sku": row["sku"],
                "units": row["units"],
                "revenue": str(row["revenue"]),
            }
            # Cost columns only for somebody who may see margins - the
            # selector builds different rows depending on this flag.
            for row in top_products(
                start, end, limit=20, include_cost=user.can_view_profit, user=user
            )
        ],
        # A one-line table of your own total helps nobody, so it is sent only
        # to someone who can see more than their own records.
        "by_staff": (
            sales_by_staff(start, end, user=user)
            if user.data_scope in ("ALL", "TEAM")
            else []
        ),
        "can_export": user.has_access("report.export"),
    }
    # by_staff carries Decimals; stringify them the same way as everything else
    # so the app never has to guess whether a field is a number or text.
    payload["by_staff"] = [
        {**row, "revenue": str(row["revenue"]), "collected": str(row["collected"]),
         "outstanding": str(row["outstanding"])}
        for row in payload["by_staff"]
    ]
    return Response(payload)


@api_view(["GET"])
@permission_classes([requires("report.inventory")])
def inventory_report(request):
    """What is on the shelf, what it is worth, and what is running out."""
    user = request.user
    alive = scoped(Product.objects.alive(), user)
    products = alive.filter(is_active=True).select_related("category")

    payload = {
        "product_count": products.count(),
        "low_stock": alive.low_stock().count(),
        "out_of_stock": alive.out_of_stock().count(),
        "products": [
            {
                "id": p.pk,
                "name": p.name,
                "sku": p.sku,
                "category_name": p.category.name if p.category_id else None,
                "stock_quantity": p.stock_quantity,
                "low_stock_threshold": p.low_stock_threshold,
                "stock_status": p.stock_status,
                "stock_status_label": p.stock_status_label,
                "selling_price": str(p.selling_price),
                # Cost is a separate permission from seeing the shelf.
                **(
                    {"cost_price": str(p.cost_price),
                     "stock_value": str(p.stock_value)}
                    if user.can_view_costs
                    else {}
                ),
            }
            for p in products.order_by("stock_quantity")[:ROW_CAP]
        ],
        "recent_movements": StockMovementSerializer(
            scoped(StockMovement.objects.all(), user)
            .select_related("product", "performed_by")[:40],
            many=True,
        ).data,
    }
    if user.can_view_costs:
        payload["valuation"] = _money_map(
            {k: v for k, v in inventory_valuation(user=user).items()
             if k not in ("units", "skus")}
        ) | {
            "units": inventory_valuation(user=user)["units"],
            "skus": inventory_valuation(user=user)["skus"],
        }
    return Response(payload)


@api_view(["GET"])
@permission_classes([requires("report.receivables")])
def receivables_report(request):
    """Who owes what, how late, and who owes the most."""
    user = request.user
    receivables = receivables_summary(user=user)

    debts = (
        scoped(DebtRecord.objects.all(), user)
        .open_debts()
        .select_related("customer", "transaction")
        .order_by("due_date")[:ROW_CAP]
    )
    top_debtors = (
        scoped(CreditAccount.objects.in_debt(), user)
        .select_related("customer")
        .order_by("-outstanding_balance")[:20]
    )

    return Response(
        {
            "outstanding": str(receivables["outstanding"]),
            "overdue_amount": str(receivables["overdue_amount"]),
            "open_count": receivables["debt_count"],
            "overdue_count": receivables["overdue_count"],
            "aging": {
                "buckets": _money_map(receivables["aging"]["buckets"]),
                "counts": receivables["aging"]["counts"],
                "total": str(receivables["aging"]["total"]),
            },
            "debts": DebtSerializer(debts, many=True).data,
            "top_debtors": [
                {
                    "customer_id": a.customer_id,
                    "name": a.customer.name,
                    "phone": a.customer.phone,
                    "outstanding": str(a.outstanding_balance),
                    "credit_limit": str(a.credit_limit),
                    "risk_level": a.risk_level,
                    "risk_label": a.risk_label,
                    "is_blocked": a.is_blocked,
                }
                for a in top_debtors
            ],
            "can_export": user.has_access("report.export"),
        }
    )


@api_view(["GET"])
@permission_classes([requires("credit.view")])
def borrowers(request):
    """
    Everyone who has ever bought on credit, searchable.

    Separate from the receivables report because that answers "how exposed are
    we" while this answers "who is this person and what do they owe" - the
    question somebody asks with a customer standing in front of them.
    """
    user = request.user
    accounts = scoped(
        CreditAccount.objects.select_related("customer"), user
    )

    q = (request.query_params.get("q") or "").strip()
    if q:
        accounts = accounts.filter(
            Q(customer__name__icontains=q) | Q(customer__phone__icontains=q)
        )
    state = (request.query_params.get("filter") or "").strip()
    if state == "owing":
        accounts = accounts.filter(outstanding_balance__gt=0)
    elif state == "blocked":
        accounts = accounts.filter(is_blocked=True)
    elif state == "over_limit":
        accounts = accounts.over_limit()

    rows = accounts.order_by("-outstanding_balance", "customer__name")[:ROW_CAP]
    return Response(
        {
            "count": accounts.count(),
            "borrowers": [
                {
                    "customer_id": a.customer_id,
                    "name": a.customer.name,
                    "phone": a.customer.phone,
                    "outstanding": str(a.outstanding_balance),
                    "credit_limit": str(a.credit_limit),
                    "available_credit": str(a.available_credit),
                    "utilisation_percent": str(a.utilisation_percent),
                    "risk_level": a.risk_level,
                    "risk_label": a.risk_label,
                    "is_blocked": a.is_blocked,
                    "block_reason": a.block_reason,
                    "last_payment_date": (
                        a.last_payment_date.isoformat() if a.last_payment_date else None
                    ),
                }
                for a in rows
            ],
        }
    )


# ---------------------------------------------------------------------------
# CSV export
# ---------------------------------------------------------------------------
def _csv_response(filename: str) -> HttpResponse:
    response = HttpResponse(content_type="text/csv")
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    return response


@api_view(["GET"])
@permission_classes([requires("report.export")])
def export_sales(request):
    """
    The same CSV the web app writes, for the same people.

    Cost and profit columns are written only for someone holding
    `report.profit`. The filter is on the writer, not on the link, so a sales
    assistant who reaches this URL gets a file with the operational columns
    and nothing else rather than a refusal.
    """
    start, end = _period(request)
    user = request.user
    show_financials = user.can_view_profit

    response = _csv_response(f"sales_{start}_{end}.csv")
    writer = csv.writer(response)
    header = [
        "Reference", "Date", "Customer", "Phone", "Items", "Subtotal",
        "Discount", "Tax", "Total", "Paid", "Balance", "Status", "Method",
        "Sold by",
    ]
    if show_financials:
        header += ["Cost of goods", "Gross profit", "Margin %"]
    writer.writerow(header)

    transactions = (
        scoped(Transaction.objects.active(), user)
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
        description=(
            f"Exported sales CSV for {start} to {end} from the mobile app "
            f"({'with' if show_financials else 'without'} financials)."
        ),
        user=user,
        request=request,
    )
    return response


@api_view(["GET"])
@permission_classes([requires("report.export")])
def export_receivables(request):
    user = request.user
    response = _csv_response(f"receivables_{timezone.localdate()}.csv")
    writer = csv.writer(response)
    writer.writerow([
        "Debt ref", "Sale ref", "Customer", "Phone", "Issued", "Due",
        "Principal", "Repaid", "Balance", "Status", "Days overdue",
        "Aging bucket",
    ])
    for debt in (
        scoped(DebtRecord.objects.all(), user)
        .open_debts()
        .select_related("customer", "transaction")
        .order_by("due_date")
    ):
        writer.writerow([
            debt.reference,
            debt.transaction.reference if debt.transaction_id else "",
            debt.customer.name, debt.customer.phone,
            debt.issued_date, debt.due_date,
            debt.principal, debt.amount_repaid, debt.balance,
            debt.get_status_display(), debt.days_overdue, debt.aging_bucket,
        ])

    log_action(
        AuditAction.EXPORT,
        description="Exported accounts-receivable CSV from the mobile app.",
        user=user,
        request=request,
    )
    return response


# ---------------------------------------------------------------------------
# Audit trail
# ---------------------------------------------------------------------------
def _audit_rows(queryset, limit=ROW_CAP):
    return [
        {
            "id": entry.pk,
            "action": entry.action,
            "action_display": entry.get_action_display(),
            "model_name": entry.model_name,
            "object_repr": entry.object_repr,
            "description": entry.description,
            "user": entry.user.display_name if entry.user_id else entry.username_snapshot,
            "ip_address": entry.ip_address,
            "created_at": entry.created_at.isoformat(),
        }
        for entry in queryset[:limit]
    ]


@api_view(["GET"])
@permission_classes([requires("audit.view")])
def audit_log(request):
    """
    Everyone's actions. The permanent record, filtered but never edited.

    Deliberately not a ModelViewSet: AuditLog has no writes, no updates and no
    deletes, and exposing it through one would put a DELETE route on the
    router that the model itself raises on.
    """
    qs = AuditLog.objects.select_related("user")

    action = (request.query_params.get("action") or "").strip()
    model_name = (request.query_params.get("model") or "").strip()
    user_id = (request.query_params.get("user") or "").strip()
    q = (request.query_params.get("q") or "").strip()

    if action:
        qs = qs.filter(action=action)
    if model_name:
        qs = qs.filter(model_name=model_name)
    if user_id.isdigit():
        qs = qs.filter(user_id=int(user_id))
    if q:
        qs = qs.filter(
            Q(description__icontains=q)
            | Q(object_repr__icontains=q)
            | Q(username_snapshot__icontains=q)
        )

    return Response(
        {
            "count": qs.count(),
            "entries": _audit_rows(qs),
            # `action_display`, not `label`: the renderer translates keys that
            # end in _display or _label, and a bare "label" would either miss
            # the translation or force the rule to be widened to a key generic
            # enough to catch a chart's date labels one day.
            "actions": [
                {"value": value, "action_display": label}
                for value, label in AuditAction.choices
            ],
        }
    )


@api_view(["GET"])
@permission_classes([])
def my_activity(request):
    """
    Your own trail, and nobody else's.

    No permission required beyond being signed in: everyone is entitled to see
    what their own account did, and refusing it would make the audit log feel
    like something done *to* people rather than a record they are part of.
    """
    qs = AuditLog.objects.filter(user=request.user).select_related("user")
    return Response({"count": qs.count(), "entries": _audit_rows(qs, limit=100)})


# ---------------------------------------------------------------------------
# Registration security
# ---------------------------------------------------------------------------
#: Same floor as the web screen. Short by password standards on purpose - this
#: is read aloud to a new hire - but long enough that the throttle makes
#: guessing hopeless.
MIN_PASSCODE_LENGTH = 6

OBVIOUS_PASSCODES = frozenset(
    {"123456", "1234567", "12345678", "password", "passcode",
     "admin123", "000000", "111111", "abcdef", "qwerty"}
)


def _registration_payload() -> dict:
    """
    The state of every role's registration door.

    Never includes a passcode. They are stored hashed and cannot be read back
    by anyone - including whoever set them - so the only honest answer is
    whether one exists.
    """
    ensure_passcode_rows()
    return {
        "allow_self_registration": SystemSetting.load().allow_self_registration,
        "min_length": MIN_PASSCODE_LENGTH,
        "roles": [
            {
                "code": row["role"].code,
                "name": row["role"].name,
                "is_system": row["is_system"],
                "enabled": row["enabled"],
                "has_passcode": row["has_passcode"],
                "from_server": row["from_env"],
                "available": row["available"],
                "users": row["users"],
                "use_count": row["use_count"],
                "note": row["note"],
                "last_used_at": (
                    row["last_used_at"].isoformat() if row["last_used_at"] else None
                ),
            }
            for row in registration_status()
        ],
    }


@api_view(["GET", "POST"])
@permission_classes([requires("settings.view")])
def registration_security(request):
    """
    Which roles somebody may sign themselves up as, and with what code.

    GET reports the state; POST sets or clears codes and switches. The same
    rules as the web screen, in the same order, because a passcode that
    behaves differently depending on which client set it is a trap.
    """
    conf = SystemSetting.load()

    if request.method == "GET":
        return Response(_registration_payload())

    if not request.user.has_access("settings.edit"):
        return Response(
            {"detail": "You do not have permission to change system settings."},
            status=403,
        )

    payload = request.data or {}
    errors: dict[str, str] = {}
    changes: list[str] = []

    if "allow_self_registration" in payload:
        allow = bool(payload["allow_self_registration"])
        if conf.allow_self_registration != allow:
            conf.allow_self_registration = allow
            conf.updated_by = request.user
            conf.save()
            changes.append("self-registration turned " + ("on" if allow else "off"))

    ensure_passcode_rows()
    for entry in payload.get("roles", []) or []:
        code = str(entry.get("code", "")).strip()
        role = RoleDefinition.objects.filter(code=code).first()
        if role is None:
            continue

        row, _ = RegistrationPasscode.objects.get_or_create(role_code=code)
        raw = str(entry.get("passcode") or "").strip()
        clearing = bool(entry.get("clear"))
        wanted_on = bool(entry.get("enabled", row.is_enabled))

        if clearing:
            row.set_passcode("")  # also switches the role off
            wanted_on = False
            changes.append(f"cleared the {role.name} passcode")
        elif raw:
            if len(raw) < MIN_PASSCODE_LENGTH:
                errors[code] = f"Use at least {MIN_PASSCODE_LENGTH} characters."
                continue
            if raw.lower() in OBVIOUS_PASSCODES:
                errors[code] = (
                    "That code is one of the first things anyone would try. "
                    "Pick something else."
                )
                continue
            row.set_passcode(raw)
            changes.append(f"set a new {role.name} passcode")

        if wanted_on and not (row.has_passcode or has_server_passcode(code)):
            errors[code] = (
                "Set a passcode first - a role cannot be opened for "
                "registration without one."
            )
            wanted_on = False

        if row.is_enabled != wanted_on:
            row.is_enabled = wanted_on
            changes.append(
                f"{role.name} registration turned " + ("on" if wanted_on else "off")
            )

        if "note" in entry:
            row.note = str(entry.get("note") or "")[:120]

        row.updated_by = request.user
        row.save()

    if changes:
        # Names what changed, never the code itself: the audit log is read by
        # more people than the settings screen is.
        log_action(
            AuditAction.UPDATE,
            instance=conf,
            description="Registration security: " + "; ".join(changes) + ".",
            user=request.user,
            request=request,
        )

    if errors:
        # 400 with a per-role map, so the app can put each message under the
        # box it belongs to instead of showing one banner for four rows.
        return Response({"errors": errors}, status=400)

    # Answer with the fresh state, so the app does not have to re-fetch to
    # find out whether the switch it just flipped actually stuck.
    return Response(_registration_payload() | {"changed": changes})


# ---------------------------------------------------------------------------
# Overview for the reports hub
# ---------------------------------------------------------------------------
@api_view(["GET"])
@permission_classes([])
def report_index(request):
    """
    Which reports this person may open, and one headline figure for each.

    Exists so the phone's report hub can be built from the server's answer
    rather than from a list in the app that goes stale the moment a permission
    is granted. Every entry the app receives is one it can actually open.
    """
    user = request.user
    today = timezone.localdate()
    month_start = today.replace(day=1)
    cards = []

    if user.has_access("report.sales"):
        stats = sales_summary(month_start, today, user=user)
        cards.append(
            {
                "key": "sales",
                "permission": "report.sales",
                "headline": str(stats["revenue"]),
                "is_money": True,
                "count": stats["count"],
            }
        )
    if user.has_access("report.profit"):
        profit = profit_summary(month_start, today, user=user)
        cards.append(
            {
                "key": "profit",
                "permission": "report.profit",
                "headline": str(profit["gross_profit"]),
                "is_money": True,
                "count": None,
            }
        )
    if user.has_access("report.inventory"):
        products = scoped(Product.objects.alive(), user).filter(is_active=True)
        cards.append(
            {
                "key": "inventory",
                "permission": "report.inventory",
                "headline": str(products.count()),
                "is_money": False,
                "count": scoped(Product.objects.alive(), user)
                .needs_attention()
                .count(),
            }
        )
    if user.has_access("report.receivables"):
        receivables = receivables_summary(user=user)
        cards.append(
            {
                "key": "receivables",
                "permission": "report.receivables",
                "headline": str(receivables["outstanding"]),
                "is_money": True,
                "count": receivables["debt_count"],
            }
        )

    return Response(
        {
            "cards": cards,
            "can_export": user.has_access("report.export"),
            "collected_this_month": str(
                collections_summary(month_start, today, user=user)["collected"]
            ),
            "scope_is_global": sees_everything(user),
        }
    )
