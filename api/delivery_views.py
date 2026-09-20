"""
Hand-overs, for the phone and for anything else that talks to the API.

    GET  /api/deliveries/?sale=12        every hand-over of one sale
    GET  /api/deliveries/?today=true     what went out of the gate today
    POST /api/deliveries/                hand over (all, or some lines)
    POST /api/deliveries/<id>/void/      take one back

The queue itself - sales with goods still waiting - is the sales list with a
filter (/api/sales/?delivery=open), because it IS a list of sales: the stock
keeper needs the customer, the lines and what each is still owed, and the
sale serializer already sends exactly that.
"""
import datetime as dt

from django.core.exceptions import ValidationError
from rest_framework import mixins, status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from accounts.models import AuditAction
from accounts.services import log_action
from core.scoping import scoped
from sales.delivery import DeliveryError, record_delivery, void_delivery
from sales.models import Delivery, Transaction

from .permissions import ActionPermission
from .serializers import (
    DeliveryCreateSerializer,
    DeliverySerializer,
    ReasonSerializer,
    TransactionSerializer,
)
from .views import StandardPagination, _error


def _date(value):
    try:
        return dt.date.fromisoformat((value or "")[:10])
    except ValueError:
        return None


class DeliveryViewSet(
    mixins.ListModelMixin, mixins.RetrieveModelMixin, viewsets.GenericViewSet
):
    serializer_class = DeliverySerializer
    permission_classes = [ActionPermission]
    permission_map = {"GET": "delivery.view", "POST": "delivery.record"}
    action_permissions = {"void": "delivery.void"}
    pagination_class = StandardPagination

    def get_queryset(self):
        qs = scoped(
            Delivery.objects.select_related(
                "transaction", "transaction__customer", "delivered_by",
                "voided_by", "note_tag",
            ).prefetch_related("lines__item"),
            self.request.user,
        )
        params = self.request.query_params
        if params.get("sale"):
            qs = qs.filter(transaction_id=params["sale"])
        if params.get("today") == "true":
            from django.utils import timezone

            qs = qs.filter(delivered_at__date=timezone.localdate())
        start, end = _date(params.get("from")), _date(params.get("to"))
        if start:
            qs = qs.filter(delivered_at__date__gte=start)
        if end:
            qs = qs.filter(delivered_at__date__lte=end)
        if params.get("mine") == "true":
            qs = qs.filter(delivered_by=self.request.user)
        state = params.get("state")
        if state == "active":
            qs = qs.filter(is_voided=False)
        elif state == "voided":
            qs = qs.filter(is_voided=True)
        return qs.order_by("-delivered_at", "-id")

    def create(self, request):
        serializer = DeliveryCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        # Scoped: a sale this person may not see is simply not found.
        sale = scoped(Transaction.objects.all(), request.user).filter(
            pk=data["sale"]
        ).first()
        if sale is None:
            return Response({"detail": "Sale not found."}, status=404)

        try:
            delivery = record_delivery(
                sale,
                user=request.user,
                lines=data.get("lines") or [],
                everything=data.get("everything", False),
                received_by_name=data.get("received_by_name", ""),
                received_by_phone=data.get("received_by_phone", ""),
                vehicle=data.get("vehicle", ""),
                notes=data.get("notes", ""),
                note_tag=data.get("note_tag"),
            )
        except (DeliveryError, ValidationError) as exc:
            return _error(exc)

        units = sum(line.quantity for line in delivery.lines.all())
        log_action(
            AuditAction.CREATE,
            instance=delivery,
            description=(
                f"Handed over {units} unit(s) of {sale.reference} "
                f"to {delivery.received_by_name or sale.customer_display}."
            ),
        )
        sale.refresh_from_db()
        context = {"request": request}
        return Response(
            {
                "delivery": DeliverySerializer(delivery, context=context).data,
                "sale": TransactionSerializer(sale, context=context).data,
            },
            status=status.HTTP_201_CREATED,
        )

    @action(detail=True, methods=["post"])
    def void(self, request, pk=None):
        delivery = self.get_object()
        serializer = ReasonSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            delivery = void_delivery(
                delivery,
                user=request.user,
                reason=serializer.validated_data["reason"],
            )
        except (DeliveryError, ValidationError) as exc:
            return _error(exc)

        log_action(
            AuditAction.VOID,
            instance=delivery,
            description=(
                f"Cancelled hand-over {delivery.reference} of "
                f"{delivery.transaction.reference}: "
                f"{serializer.validated_data['reason']}"
            ),
        )
        return Response(DeliverySerializer(delivery, context={"request": request}).data)
