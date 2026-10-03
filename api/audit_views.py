"""
The Audit, for the phone.

One GET returns the whole page - the app draws every chart from it - so a
slow connection in the yard costs one round trip, not ten. The owner's cost
for a product is set with a POST to the product's own address, which answers
with the product's history so the edit sheet can show what it replaced.

Everything is worked out in reports/audit.py; the web page uses the same
functions, so the two can never disagree about a figure.
"""
import datetime as dt
from decimal import Decimal

from django.shortcuts import get_object_or_404
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.response import Response

from core.scoping import scoped
from inventory.models import Product
from reports.audit import CostError, build_report, resolve_period, set_your_cost
from reports.audit_detail import KINDS, build_detail

from .permissions import requires


def jsonable(value):
    """Decimals as strings (as everywhere else in this API), dates as ISO."""
    if isinstance(value, Decimal):
        return f"{value:f}"
    if isinstance(value, (dt.date, dt.datetime)):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [jsonable(v) for v in value]
    return value


@api_view(["GET"])
@permission_classes([requires("costing.view")])
def audit_report(request):
    """
    ?range=30d|90d|365d|all, or ?date_from=&date_to=. ?product= picks whose
    batch costs the cost candles follow.
    """
    q = request.query_params
    period = resolve_period(
        q.get("range", ""), q.get("date_from", ""), q.get("date_to", ""),
        user=request.user,
    )
    report = build_report(request.user, period, product_id=q.get("product"))
    return Response(jsonable(report))


@api_view(["GET"])
@permission_classes([requires("costing.view")])
def audit_detail(request, kind):
    """
    One Audit card opened up: money-out, money-in, profit or on-hand, for the
    same period parameters as the Audit itself. Its total is the card's total.
    """
    if kind not in KINDS:
        return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)
    q = request.query_params
    period = resolve_period(
        q.get("range", ""), q.get("date_from", ""), q.get("date_to", ""),
        user=request.user,
    )
    # q, type, by, state, min, max, sort narrow the list of payments - see
    # reports.audit_detail.filter_lines.
    return Response(jsonable(build_detail(request.user, period, kind, filters=q)))


@api_view(["POST"])
@permission_classes([requires("costing.view")])
def correct_delivery(request, source, pk):
    """
    Put right a delivery ("material") or a restock ("product") that was
    entered wrong: {"quantity": "4", "unit_cost": "3750", "note": "...",
    "change_stock": false}. A quantity of 0 means it never happened. See
    reports/corrections.py for why the stock only changes when asked.
    """
    from reports.corrections import CorrectionError, can_correct
    from reports.corrections import correct_delivery as correct

    if not can_correct(request.user, source):
        return Response(
            {"detail": "You may not correct deliveries."},
            status=status.HTTP_403_FORBIDDEN,
        )
    data = request.data or {}
    raw_flag = data.get("change_stock", False)
    change_stock = raw_flag is True or str(raw_flag).lower() in ("1", "true", "yes", "on")
    try:
        row = correct(
            source, pk, user=request.user,
            quantity=data.get("quantity", ""),
            unit_cost=data.get("unit_cost"),
            note=data.get("note", ""),
            change_stock=change_stock,
            request=request,
        )
    except CorrectionError as exc:
        return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
    return Response(jsonable({
        "id": row.pk,
        "source": source,
        "corrected": row.is_corrected,
        "quantity": row.counted_quantity,
        "unit_cost": row.counted_unit_cost,
        "recorded_quantity": row.quantity_delta,
        "recorded_unit_cost": row.unit_cost,
    }))


def _history(product, limit=10):
    return [
        {
            "cost": e.cost,
            "previous": e.previous,
            "system_cost": e.system_cost,
            "note": e.note,
            "set_by": e.set_by.display_name if e.set_by_id else "",
            "set_at": e.set_at,
        }
        for e in product.cost_estimates.select_related("set_by")[:limit]
    ]


@api_view(["GET", "POST"])
@permission_classes([requires("costing.view")])
def product_cost(request, pk):
    """
    GET: the owner's figure for one product and how it got there.
    POST {"cost": "24.50" | null, "note": "..."}: set or clear it - which
    takes `costing.set`, the owner's own call.
    """
    product = get_object_or_404(scoped(Product.objects.alive(), request.user), pk=pk)

    if request.method == "POST":
        if not request.user.has_access("costing.set"):
            return Response(
                {"detail": "Only the owner can set what a product costs."},
                status=status.HTTP_403_FORBIDDEN,
            )
        data = request.data or {}
        try:
            set_your_cost(
                product, data.get("cost"), user=request.user,
                note=data.get("note", ""), request=request,
            )
        except CostError as exc:
            return Response({"cost": [str(exc)]}, status=status.HTTP_400_BAD_REQUEST)
        product.refresh_from_db()

    return Response(jsonable({
        "id": product.pk,
        "name": product.name,
        "your_cost": product.audit_cost,
        "your_cost_note": product.audit_cost_note,
        "your_cost_set_at": product.audit_cost_set_at,
        "system_cost": product.cost_price,
        "selling_price": product.selling_price,
        "history": _history(product),
    }))
