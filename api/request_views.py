"""
Production requests: the counter asking the yard for more stock.

WHY THIS IS NOT THE LOW-STOCK ALERT
-----------------------------------
api/signals.py already buzzes the owner when a product crosses its threshold.
That message says something is nearly gone. It does not say how many are
wanted, who is waiting on them, or whether anybody agreed to pour them - so it
gets read on a phone, half-remembered, and the seller finds out at the counter
that nothing was made.

A request is the missing half, and it is addressed to a person by name. It
outlives the notification, it can be accepted or declined, and "I told them
last week" becomes something that can be checked instead of argued about.

WHO SEES WHAT
-------------
Deliberately not core.scoping.scoped(). A request is a conversation between
two named people, so the useful filter is "mine" - raised by me, or addressed
to me. An administrator sees all of them, because chasing the ones nobody
answered is exactly their job.
"""
from django.core.exceptions import ValidationError
from django.db.models import Q
from rest_framework import viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from accounts.models import AuditAction
from accounts.services import log_action
from core.scoping import scoped
from inventory.models import Product
from production.models import ProductionRequest, RequestStatus
from production.services import (
    assignable_deciders,
    cancel_request,
    request_production,
    respond_to_request,
)

from .permissions import ActionPermission
from .production_serializers import (
    ProductionRequestCreateSerializer,
    ProductionRequestSerializer,
    RequestResponseSerializer,
)
from .views import StandardPagination, _error


class ProductionRequestViewSet(viewsets.ModelViewSet):
    serializer_class = ProductionRequestSerializer
    permission_classes = [ActionPermission]
    permission_map = {
        # Either half of the conversation may read the list.
        "GET": ["production.request", "production.approve"],
        "POST": "production.request",
        "DELETE": "production.request",
    }
    action_permissions = {
        "respond": "production.approve",
        "cancel": "production.request",
        # Open on purpose: the picker has to list people the asker may not
        # otherwise be allowed to look up, and it returns a name and a role.
        "deciders": "*",
        "summary": "*",
    }
    pagination_class = StandardPagination
    http_method_names = ["get", "post", "head", "options"]

    def get_queryset(self):
        user = self.request.user
        qs = ProductionRequest.objects.select_related(
            "product", "requested_by", "assigned_to", "fulfilled_run"
        )
        if not user.is_admin:
            qs = qs.filter(Q(requested_by=user) | Q(assigned_to=user))

        params = self.request.query_params
        box = (params.get("box") or "").strip()
        if box == "incoming":
            qs = qs.filter(assigned_to=user)
        elif box == "sent":
            qs = qs.filter(requested_by=user)

        status_filter = (params.get("status") or "").strip().upper()
        if status_filter == "OPEN":
            qs = qs.open()
        elif status_filter:
            qs = qs.filter(status=status_filter)

        if params.get("product"):
            qs = qs.filter(product_id=params["product"])
        return qs

    def create(self, request, *args, **kwargs):
        serializer = ProductionRequestCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        # Scoped: a product the asker cannot see is simply not found, which
        # says less than "that is not yours" would.
        product = scoped(Product.objects.alive(), request.user).filter(
            pk=data["product"]
        ).first()
        if product is None:
            return Response(
                {"detail": "That product is not in your list."}, status=404
            )

        decider = next(
            (
                person
                for person in assignable_deciders(request.user)
                if person.pk == data["assigned_to"]
            ),
            None,
        )
        if decider is None:
            return Response(
                {"detail": "That person cannot record production."}, status=400
            )

        try:
            obj = request_production(
                product=product,
                quantity=data["quantity"],
                requested_by=request.user,
                assigned_to=decider,
                reason_id=data.get("reason"),
                reason_name=data.get("reason_name", ""),
                note=data.get("note", ""),
                needed_by=data.get("needed_by"),
            )
        except ValidationError as exc:
            return _error(exc)

        log_action(
            AuditAction.CREATE,
            instance=obj,
            description=(
                f"Asked {decider.display_name} for {obj.quantity} x "
                f"{product.name} from the mobile app."
            ),
        )
        return Response(
            self.get_serializer(obj, context=self.get_serializer_context()).data,
            status=201,
        )

    @action(detail=True, methods=["post"])
    def respond(self, request, pk=None):
        """Accept or decline. The person who asked is told either way."""
        obj = self.get_object()
        serializer = RequestResponseSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        try:
            obj = respond_to_request(
                obj,
                user=request.user,
                accept=serializer.validated_data["accept"],
                note=serializer.validated_data.get("note", ""),
            )
        except ValidationError as exc:
            return _error(exc)

        log_action(
            AuditAction.UPDATE,
            instance=obj,
            description=(
                f"{obj.get_status_display()} the request for {obj.quantity} x "
                f"{obj.product.name}."
            ),
        )
        return Response(self.get_serializer(obj).data)

    @action(detail=True, methods=["post"])
    def cancel(self, request, pk=None):
        """Withdraw a request that is no longer needed."""
        obj = self.get_object()
        try:
            obj = cancel_request(
                obj, user=request.user, note=request.data.get("note", "")
            )
        except ValidationError as exc:
            return _error(exc)

        log_action(
            AuditAction.UPDATE,
            instance=obj,
            description=f"Cancelled the request for {obj.product.name}.",
        )
        return Response(self.get_serializer(obj).data)

    @action(detail=False, methods=["get"])
    def deciders(self, request):
        """
        Who this request may be addressed to.

        Everyone who can actually record a batch. Sending it to somebody
        without that permission produces a notification they cannot act on,
        which is worse than none: they assume it is handled and so does the
        sender.
        """
        return Response([
            {
                "id": person.pk,
                "name": person.display_name,
                "role": person.get_role_display(),
                "is_admin": person.is_admin,
                "phone": person.phone,
            }
            for person in assignable_deciders(request.user)
        ])

    @action(detail=False, methods=["get"])
    def summary(self, request):
        """Badge counts for the tab bar."""
        user = request.user
        base = ProductionRequest.objects.all()
        return Response({
            "incoming_pending": base.filter(
                assigned_to=user, status=RequestStatus.PENDING
            ).count(),
            "sent_open": base.filter(requested_by=user).open().count(),
        })
