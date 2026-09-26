"""
The phone's half of the yard.

Same permissions, same scoping and the same services as the web screens - the
two are different front doors onto one building, not two implementations.
"""
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db.models import Q
from rest_framework import viewsets
from rest_framework.decorators import action, api_view, permission_classes
from rest_framework.response import Response

from accounts.models import AuditAction
from accounts.services import log_action
from core.scoping import scoped
from inventory.models import Product
from production.models import (
    MaterialMovement,
    ProductionRun,
    RawMaterial,
    Recipe,
    RecipeItem,
)
from production.services import (
    plan_for,
    receive_material,
    recount_material,
    record_production,
    return_material_to_supplier,
    reverse_production,
    waste_material,
)

from .permissions import ActionPermission, HasPermission, requires
from .production_serializers import (
    MaterialAdjustSerializer,
    MaterialMovementSerializer,
    MaterialReceiveSerializer,
    ProductionRunCreateSerializer,
    ProductionRunSerializer,
    RawMaterialSerializer,
    RecipeSerializer,
    ReversalSerializer,
)
from .views import StandardPagination, _error


class RawMaterialViewSet(viewsets.ModelViewSet):
    serializer_class = RawMaterialSerializer
    permission_classes = [ActionPermission]
    permission_map = {
        "GET": "material.view",
        "POST": "material.create",
        "PATCH": "material.edit",
        "PUT": "material.edit",
        "DELETE": "material.edit",
    }
    action_permissions = {
        "receive": "material.receive",
        "adjust": "material.adjust",
        "movements": "material.view",
        "used_in": "material.view",
        "low": "material.view",
    }
    pagination_class = StandardPagination

    def get_queryset(self):
        qs = scoped(
            RawMaterial.objects.alive().select_related("supplier", "owner"),
            self.request.user,
        )
        params = self.request.query_params

        q = params.get("q", "").strip()
        if q:
            qs = qs.filter(Q(name__icontains=q) | Q(code__icontains=q))

        level = params.get("level", "")
        if level == "low":
            qs = qs.low()
        elif level == "out":
            qs = qs.empty()
        elif level == "order":
            qs = qs.needs_ordering()

        if params.get("active") == "true":
            qs = qs.filter(is_active=True)
        return qs.order_by("name")

    def perform_create(self, serializer):
        obj = serializer.save(
            owner=self.request.user,
            created_by=self.request.user,
            updated_by=self.request.user,
        )
        log_action(
            AuditAction.CREATE, instance=obj,
            description=f"Added raw material '{obj.name}' from the mobile app.",
        )

    def perform_update(self, serializer):
        obj = serializer.save(updated_by=self.request.user)
        log_action(
            AuditAction.UPDATE, instance=obj,
            description=f"Edited raw material '{obj.name}' from the mobile app.",
        )

    def perform_destroy(self, instance):
        instance.soft_delete(user=self.request.user)
        log_action(
            AuditAction.DELETE, instance=instance,
            description=f"Archived raw material '{instance.name}'.",
        )

    @action(detail=False, methods=["get"])
    def low(self, request):
        qs = self.get_queryset().needs_ordering().order_by("quantity_in_stock")
        page = self.paginate_queryset(qs)
        return self.get_paginated_response(self.get_serializer(page, many=True).data)

    @action(detail=True, methods=["post"])
    def receive(self, request, pk=None):
        """A delivery arrives."""
        material = self.get_object()
        serializer = MaterialReceiveSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        unit_cost = serializer.validated_data.get("unit_cost")
        if unit_cost is not None and not request.user.can_view_costs:
            # Somebody who may receive stock but not see costs cannot set one
            # either. The field is dropped rather than the request refused, so
            # the delivery still gets recorded.
            unit_cost = None

        try:
            movement = receive_material(
                material,
                serializer.validated_data["quantity"],
                user=request.user,
                unit_cost=unit_cost,
                reference=serializer.validated_data.get("reference", ""),
                reason=serializer.validated_data.get("reason", ""),
            )
        except ValidationError as exc:
            return _error(exc)

        log_action(
            AuditAction.STOCK, instance=material,
            description=(
                f"Received {movement.quantity_delta} {material.unit} of "
                f"'{material.name}' from the mobile app."
            ),
        )
        return Response(MaterialMovementSerializer(movement).data, status=201)

    @action(detail=True, methods=["post"])
    def adjust(self, request, pk=None):
        """Waste, a supplier return, or a counted figure."""
        material = self.get_object()
        serializer = MaterialAdjustSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        kind = serializer.validated_data["kind"]
        quantity = serializer.validated_data["quantity"]
        reason = serializer.validated_data.get("reason", "")

        try:
            if kind == "RECOUNT":
                movement = recount_material(
                    material, quantity, user=request.user, reason=reason
                )
                if movement is None:
                    # Not an error, and not worth a ledger row.
                    return Response(
                        {"detail": "The count already matches. Nothing changed.",
                         "changed": False},
                        status=200,
                    )
            elif kind == "RETURN_OUT":
                movement = return_material_to_supplier(
                    material, quantity, user=request.user, reason=reason
                )
            else:
                movement = waste_material(
                    material, quantity, user=request.user, reason=reason
                )
        except ValidationError as exc:
            return _error(exc)

        log_action(
            AuditAction.STOCK, instance=material,
            description=f"Corrected '{material.name}' ({kind}) from the mobile app.",
        )
        return Response(MaterialMovementSerializer(movement).data, status=201)

    @action(detail=True, methods=["get"])
    def movements(self, request, pk=None):
        qs = self.get_object().movements.select_related("performed_by")[:100]
        return Response(MaterialMovementSerializer(qs, many=True).data)

    @action(detail=True, methods=["get"], url_path="used-in")
    def used_in(self, request, pk=None):
        """
        Which products are made from this material.

        The web material page answers the same question under the same
        permission. It is deliberately NOT gated on `recipe.manage`: knowing
        that cement goes into hollow blocks is store knowledge, not recipe
        authorship, and a storeman about to write something off should be able
        to see what he is about to starve.
        """
        material = self.get_object()
        rows = (
            RecipeItem.objects.filter(material=material)
            .select_related("recipe__product")
            .order_by("recipe__product__name")
        )
        return Response([
            {
                "product": item.recipe.product_id,
                "product_name": item.recipe.product.name,
                "quantity": str(item.quantity),
                "output_quantity": item.recipe.output_quantity,
                "unit_display": material.get_unit_display(),
            }
            for item in rows
        ])


class MaterialMovementViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class = MaterialMovementSerializer
    permission_classes = [HasPermission]
    required_permission = "material.view"
    pagination_class = StandardPagination

    def get_queryset(self):
        qs = scoped(
            MaterialMovement.objects.select_related("material", "performed_by"),
            self.request.user,
        )
        kind = self.request.query_params.get("type")
        if kind:
            qs = qs.filter(movement_type=kind)
        material = self.request.query_params.get("material")
        if material:
            qs = qs.filter(material_id=material)
        return qs


class RecipeViewSet(viewsets.ModelViewSet):
    """
    Recipes, addressed by PRODUCT id rather than by their own.

    A product has at most one recipe, so "the recipe for product 12" is the
    only way anyone ever asks for it. Making the client first look up a recipe
    id would be a round trip that answers a question nobody has.
    """

    serializer_class = RecipeSerializer
    permission_classes = [ActionPermission]
    permission_map = {
        "GET": "recipe.manage",
        "POST": "recipe.manage",
        "PUT": "recipe.manage",
        "PATCH": "recipe.manage",
        "DELETE": "recipe.manage",
    }
    pagination_class = StandardPagination
    lookup_field = "product_id"
    lookup_url_kwarg = "pk"

    def get_queryset(self):
        return scoped(
            Recipe.objects.select_related("product").prefetch_related(
                "items__material"
            ),
            self.request.user,
        )

    def get_object(self):
        """
        Fetch by product, creating the shell on first write.

        A product with no recipe is a normal state, not a 404 - and "set the
        recipe" is exactly the request that should bring one into being.
        """
        product = scoped(Product.objects.alive(), self.request.user).filter(
            pk=self.kwargs["pk"]
        ).first()
        if product is None:
            from django.http import Http404

            raise Http404("No such product.")

        recipe = Recipe.objects.filter(product=product).first()
        if recipe is None:
            if self.request.method in ("GET", "DELETE"):
                from django.http import Http404

                raise Http404("This product has no recipe yet.")
            recipe = Recipe.objects.create(
                product=product,
                created_by=self.request.user,
                updated_by=self.request.user,
            )
        return recipe

    def perform_update(self, serializer):
        recipe = serializer.save(updated_by=self.request.user)
        log_action(
            AuditAction.UPDATE, instance=recipe.product,
            description=f"Set the recipe for '{recipe.product.name}'.",
        )


class ProductionRunViewSet(viewsets.ReadOnlyModelViewSet):
    """
    Runs are created through the service, never by writing columns, so this is
    read-only plus two actions rather than a ModelViewSet.
    """

    serializer_class = ProductionRunSerializer
    permission_classes = [ActionPermission]
    permission_map = {"GET": "production.view", "POST": "production.create"}
    action_permissions = {"reverse": "production.reverse"}
    pagination_class = StandardPagination

    def get_queryset(self):
        qs = scoped(
            ProductionRun.objects.select_related(
                "product", "owner", "created_by", "reversed_by"
            ).prefetch_related("materials__material", "damages", "expenses"),
            self.request.user,
        )
        params = self.request.query_params
        q = params.get("q", "").strip()
        if q:
            qs = qs.filter(
                Q(reference__icontains=q) | Q(product__name__icontains=q)
            )
        if params.get("status"):
            qs = qs.filter(status=params["status"])
        if params.get("product"):
            qs = qs.filter(product_id=params["product"])
        return qs

    def create(self, request, *args, **kwargs):
        serializer = ProductionRunCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        product = scoped(Product.objects.alive(), request.user).filter(
            pk=data["product"]
        ).first()
        if product is None:
            return Response(
                {"detail": "That product is not in your list."}, status=404
            )

        try:
            run = record_production(
                product=product,
                quantity_produced=data["quantity_produced"],
                quantity_rejected=data.get("quantity_rejected") or 0,
                damages=data.get("damages") or [],
                fulfils=data.get("fulfils") or [],
                produced_on=data.get("produced_on"),
                notes=data.get("notes", ""),
                note_tag=data.get("note_tag"),
                materials=[
                    {
                        "material": line["material"],
                        "quantity": line["quantity"],
                        "expected_quantity": line.get("expected_quantity"),
                    }
                    for line in data["materials"]
                ],
                user=request.user,
                update_product_cost=data.get("update_product_cost", True),
                expenses=[dict(line) for line in data.get("expenses") or []],
                expense_payment=dict(data.get("expense_payment") or {}),
            )
        except ValidationError as exc:
            return _error(exc)

        log_action(
            AuditAction.STOCK, instance=run,
            description=(
                f"Recorded {run.reference}: {run.quantity_produced} x "
                f"{product.name} from the mobile app."
            ),
        )
        return Response(
            self.get_serializer(run, context=self.get_serializer_context()).data,
            status=201,
        )

    @action(detail=True, methods=["post"])
    def reverse(self, request, pk=None):
        run = self.get_object()
        serializer = ReversalSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            reverse_production(
                run, user=request.user, reason=serializer.validated_data["reason"]
            )
        except ValidationError as exc:
            return _error(exc)

        run.refresh_from_db()
        log_action(
            AuditAction.STOCK, instance=run,
            description=f"Reversed {run.reference} from the mobile app.",
        )
        return Response(self.get_serializer(run).data)


@api_view(["GET"])
@permission_classes([requires("production.create")])
def production_plan(request):
    """
    What a batch would take, before anything is written.

    The phone calls this as the operator types, so a shortage is on screen
    while it can still be acted on rather than arriving as a refusal after
    they press Save.
    """
    try:
        product_id = int(request.query_params.get("product", 0))
        quantity = int(request.query_params.get("quantity", 0))
    except (TypeError, ValueError):
        return Response(
            {"detail": "Give a product and a quantity."}, status=400
        )

    product = scoped(Product.objects.alive(), request.user).filter(
        pk=product_id
    ).first()
    if product is None:
        return Response({"detail": "That product is not in your list."}, status=404)

    try:
        plan = plan_for(product, quantity, user=request.user)
    except ValidationError as exc:
        return _error(exc)

    show_cost = request.user.can_view_costs
    payload = {
        "product": product.pk,
        "product_name": product.name,
        "quantity": plan["quantity"],
        "has_recipe": plan["has_recipe"],
        "output_quantity": plan["output_quantity"],
        "can_produce": plan["can_produce"],
        "shortages": plan["shortages"],
        "lines": [
            {
                "material": line["material_id"],
                "material_name": line["material_name"],
                "material_code": line["material_code"],
                "unit_display": line["unit_display"],
                "required": str(line["required"]),
                "available": str(line["available"]),
                "is_short": line["is_short"],
                "short_by": str(line["short_by"]),
                **(
                    {
                        "unit_cost": str(line["unit_cost"]),
                        "line_cost": str(line["line_cost"]),
                    }
                    if show_cost
                    else {}
                ),
            }
            for line in plan["lines"]
        ],
    }
    if show_cost:
        payload["material_cost"] = str(plan["material_cost"])
        payload["unit_cost"] = str(plan["unit_cost"])
    return Response(payload)


@api_view(["GET"])
@permission_classes([requires("production.view")])
def production_summary(request):
    """
    The numbers the production screen leads with: what has been made, what was
    rejected, and how the store is holding up.
    """
    from django.db.models import Sum

    runs = scoped(ProductionRun.objects.completed(), request.user)
    totals = runs.aggregate(
        produced=Sum("quantity_produced"),
        rejected=Sum("quantity_rejected"),
        cost=Sum("material_cost"),
        other=Sum("other_cost"),
    )
    produced = totals["produced"] or 0
    rejected = totals["rejected"] or 0
    attempted = produced + rejected

    materials = scoped(RawMaterial.objects.alive(), request.user)
    payload = {
        "runs": runs.count(),
        "produced": produced,
        "rejected": rejected,
        "yield_percent": str(
            (Decimal(produced) * 100 / Decimal(attempted)).quantize(Decimal("0.01"))
        )
        if attempted
        else "0.00",
        "materials": materials.count(),
        "materials_to_order": materials.needs_ordering().count(),
    }
    if request.user.can_view_costs:
        payload["material_cost"] = str(totals["cost"] or Decimal("0.00"))
        payload["other_cost"] = str(totals["other"] or Decimal("0.00"))
        payload["store_value"] = str(
            sum((m.stock_value for m in materials), Decimal("0.00"))
        )
    return Response(payload)
