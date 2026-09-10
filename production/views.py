"""Web screens for the yard."""
from decimal import Decimal

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.db import DatabaseError
from django.db.models import Q, Sum
from django.http import JsonResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.generic import CreateView, DetailView, ListView, UpdateView

from accounts.models import AuditAction
from accounts.services import log_action
from core.mixins import (
    AuthorStampMixin,
    OwnerScopedMixin,
    PermissionRequiredMixin,
    get_owned_or_404,
    require,
)
from core.errors import describe
from core.scoping import scoped
from inventory.models import Product

from .forms import (
    MaterialAdjustForm,
    MaterialReceiveForm,
    ProductionRunForm,
    RawMaterialForm,
    RecipeForm,
    ReversalForm,
    parse_material_lines,
)
from .models import (
    MaterialMovement,
    ProductionRun,
    RawMaterial,
    Recipe,
    RecipeItem,
)
from .services import (
    plan_for,
    receive_material,
    recount_material,
    record_production,
    return_material_to_supplier,
    reverse_production,
    waste_material,
)



def _messages_of(exc):
    """
    The lines to show for an exception, whoever it came from.

    A ValidationError carries wording aimed at the person who triggered it, so
    each message is shown as-is. Anything else gets one summarised line and the
    traceback goes to the log.
    """
    if isinstance(exc, ValidationError):
        return list(exc.messages)
    return [describe(exc)]

# ---------------------------------------------------------------------------
# Materials
# ---------------------------------------------------------------------------
class MaterialListView(OwnerScopedMixin, PermissionRequiredMixin, ListView):
    required_permission = "material.view"
    model = RawMaterial
    template_name = "production/material_list.html"
    context_object_name = "materials"
    paginate_by = 25

    def get_queryset(self):
        qs = super().get_queryset().alive().select_related("supplier", "owner")
        params = self.request.GET
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
        self.query = q
        self.level = level
        return qs.order_by("name")

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        alive = scoped(RawMaterial.objects.alive(), self.request.user)
        ctx["query"] = self.query
        ctx["level"] = self.level
        ctx["total_count"] = alive.count()
        ctx["low_count"] = alive.needs_ordering().count()
        if self.request.user.can_view_financials:
            ctx["store_value"] = sum(
                (m.stock_value for m in alive.only(
                    "quantity_in_stock", "unit_cost"
                )),
                Decimal("0.00"),
            )
        return ctx


class MaterialDetailView(OwnerScopedMixin, PermissionRequiredMixin, DetailView):
    required_permission = "material.view"
    model = RawMaterial
    template_name = "production/material_detail.html"
    context_object_name = "material"

    def get_queryset(self):
        return super().get_queryset().alive().select_related("supplier", "owner")

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["movements"] = (
            self.object.movements.select_related("performed_by")[:60]
        )
        ctx["used_in"] = (
            RecipeItem.objects.filter(material=self.object)
            .select_related("recipe__product")
            .order_by("recipe__product__name")
        )
        return ctx


class MaterialCreateView(AuthorStampMixin, PermissionRequiredMixin, CreateView):
    required_permission = "material.create"
    model = RawMaterial
    form_class = RawMaterialForm
    template_name = "production/material_form.html"

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["user"] = self.request.user
        return kwargs

    def form_valid(self, form):
        form.instance.owner = self.request.user
        response = super().form_valid(form)
        log_action(
            AuditAction.CREATE, instance=self.object,
            description=f"Added raw material '{self.object.name}'.",
        )
        messages.success(self.request, f"{self.object.name} added to the store.")
        return response

    def get_success_url(self):
        return reverse("production:material_detail", args=[self.object.pk])


class MaterialUpdateView(OwnerScopedMixin, AuthorStampMixin,
                         PermissionRequiredMixin, UpdateView):
    required_permission = "material.edit"
    model = RawMaterial
    form_class = RawMaterialForm
    template_name = "production/material_form.html"

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["user"] = self.request.user
        return kwargs

    def form_valid(self, form):
        response = super().form_valid(form)
        log_action(
            AuditAction.UPDATE, instance=self.object,
            description=f"Edited raw material '{self.object.name}'.",
        )
        messages.success(self.request, "Material updated.")
        return response

    def get_success_url(self):
        return reverse("production:material_detail", args=[self.object.pk])


class LowMaterialView(OwnerScopedMixin, PermissionRequiredMixin, ListView):
    required_permission = "material.view"
    model = RawMaterial
    template_name = "production/material_low.html"
    context_object_name = "materials"

    def get_queryset(self):
        return (
            super().get_queryset().needs_ordering()
            .select_related("supplier")
            .order_by("quantity_in_stock")
        )


class MaterialMovementListView(OwnerScopedMixin, PermissionRequiredMixin,
                               ListView):
    required_permission = "material.view"
    model = MaterialMovement
    template_name = "production/material_movements.html"
    context_object_name = "movements"
    paginate_by = 50

    def get_queryset(self):
        qs = super().get_queryset().select_related("material", "performed_by")
        kind = self.request.GET.get("type", "")
        if kind:
            qs = qs.filter(movement_type=kind)
        self.kind = kind
        return qs

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["kind"] = self.kind
        return ctx


def material_receive(request, pk):
    blocked = require(
        request, "material.receive",
        message="You do not have permission to record deliveries.",
    )
    if blocked:
        return blocked

    material = get_owned_or_404(RawMaterial.objects.alive(), request.user, pk=pk)
    form = MaterialReceiveForm(request.POST or None)

    if request.method == "POST" and form.is_valid():
        try:
            receive_material(
                material,
                form.cleaned_data["quantity"],
                user=request.user,
                unit_cost=form.cleaned_data.get("unit_cost"),
                reference=form.cleaned_data.get("reference", ""),
                reason=form.cleaned_data.get("reason", ""),
            )
        except (ValidationError, DatabaseError) as exc:
            messages.error(request, describe(exc, context="material movement"))
        else:
            log_action(
                AuditAction.STOCK, instance=material,
                description=(
                    f"Received {form.cleaned_data['quantity']} "
                    f"{material.unit} of '{material.name}'."
                ),
            )
            messages.success(request, f"Delivery recorded for {material.name}.")
            return redirect("production:material_detail", pk=material.pk)

    return render(
        request,
        "production/material_receive.html",
        {"material": material, "form": form},
    )


def material_adjust(request, pk):
    blocked = require(
        request, "material.adjust",
        message="You do not have permission to correct material stock.",
    )
    if blocked:
        return blocked

    material = get_owned_or_404(RawMaterial.objects.alive(), request.user, pk=pk)
    form = MaterialAdjustForm(request.POST or None)

    if request.method == "POST" and form.is_valid():
        mode = form.cleaned_data["mode"]
        quantity = form.cleaned_data["quantity"]
        reason = form.cleaned_data.get("reason", "")
        try:
            if mode == "RECOUNT":
                movement = recount_material(
                    material, quantity, user=request.user, reason=reason
                )
                if movement is None:
                    messages.info(request, "The count already matches. Nothing changed.")
                    return redirect("production:material_detail", pk=material.pk)
            elif mode == "RETURN_OUT":
                return_material_to_supplier(
                    material, quantity, user=request.user, reason=reason
                )
            else:
                waste_material(material, quantity, user=request.user, reason=reason)
        except (ValidationError, DatabaseError) as exc:
            messages.error(request, describe(exc, context="material movement"))
        else:
            log_action(
                AuditAction.STOCK, instance=material,
                description=f"Corrected '{material.name}' ({mode}).",
            )
            messages.success(request, f"{material.name} corrected.")
            return redirect("production:material_detail", pk=material.pk)

    return render(
        request,
        "production/material_adjust.html",
        {"material": material, "form": form},
    )


# ---------------------------------------------------------------------------
# Recipes
# ---------------------------------------------------------------------------
class RecipeListView(PermissionRequiredMixin, ListView):
    required_permission = "recipe.manage"
    template_name = "production/recipe_list.html"
    context_object_name = "products"
    paginate_by = 25

    def get_queryset(self):
        return (
            scoped(Product.objects.alive(), self.request.user)
            .select_related("recipe")
            .prefetch_related("recipe__items__material")
            .order_by("name")
        )


def recipe_edit(request, pk):
    """
    Set what a product is made of.

    Lines arrive as parallel arrays and replace the recipe wholesale. Editing
    in place would mean matching rows by id and is a lot of machinery for a
    list that is rarely longer than six lines.
    """
    blocked = require(
        request, "recipe.manage",
        message="You do not have permission to edit recipes.",
    )
    if blocked:
        return blocked

    product = get_owned_or_404(Product.objects.alive(), request.user, pk=pk)
    recipe = Recipe.objects.filter(product=product).first()
    form = RecipeForm(request.POST or None, instance=recipe)
    store = scoped(RawMaterial.objects.active(), request.user).order_by("name")

    if request.method == "POST" and form.is_valid():
        lines, errors = parse_material_lines(request, request.user)
        if errors:
            for error in errors:
                messages.error(request, error)
        else:
            recipe = form.save(commit=False)
            recipe.product = product
            if not recipe.pk:
                recipe.created_by = request.user
            recipe.updated_by = request.user
            recipe.save()

            recipe.items.all().delete()
            RecipeItem.objects.bulk_create(
                [
                    RecipeItem(
                        recipe=recipe,
                        material=line["material"],
                        quantity=line["quantity"],
                    )
                    for line in lines
                ]
            )
            log_action(
                AuditAction.UPDATE, instance=product,
                description=f"Set the recipe for '{product.name}'.",
            )
            messages.success(request, f"Recipe saved for {product.name}.")
            return redirect("production:recipe_list")

    return render(
        request,
        "production/recipe_form.html",
        {
            "product": product,
            "recipe": recipe,
            "form": form,
            "store": store,
            "items": recipe.items.select_related("material").all() if recipe else [],
        },
    )


# ---------------------------------------------------------------------------
# Production runs
# ---------------------------------------------------------------------------
class RunListView(OwnerScopedMixin, PermissionRequiredMixin, ListView):
    required_permission = "production.view"
    model = ProductionRun
    template_name = "production/run_list.html"
    context_object_name = "runs"
    paginate_by = 25

    def get_queryset(self):
        qs = super().get_queryset().select_related("product", "owner", "created_by")
        params = self.request.GET
        q = params.get("q", "").strip()
        if q:
            qs = qs.filter(
                Q(reference__icontains=q) | Q(product__name__icontains=q)
            )
        status = params.get("status", "")
        if status:
            qs = qs.filter(status=status)
        self.query = q
        self.status = status
        return qs

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        mine = scoped(ProductionRun.objects.completed(), self.request.user)
        totals = mine.aggregate(
            produced=Sum("quantity_produced"),
            rejected=Sum("quantity_rejected"),
        )
        ctx["query"] = self.query
        ctx["status"] = self.status
        ctx["total_produced"] = totals["produced"] or 0
        ctx["total_rejected"] = totals["rejected"] or 0
        if self.request.user.can_view_financials:
            ctx["total_cost"] = mine.aggregate(c=Sum("material_cost"))["c"] or 0
        return ctx


class RunDetailView(OwnerScopedMixin, PermissionRequiredMixin, DetailView):
    required_permission = "production.view"
    model = ProductionRun
    template_name = "production/run_detail.html"
    context_object_name = "run"

    def get_queryset(self):
        return (
            super().get_queryset()
            .select_related("product", "owner", "created_by", "reversed_by")
            .prefetch_related("materials__material")
        )

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["can_reverse"] = self.request.user.has_access("production.reverse")
        ctx["reversal_form"] = ReversalForm()
        return ctx


def run_create(request):
    blocked = require(
        request, "production.create",
        message="You do not have permission to record production.",
    )
    if blocked:
        return blocked

    form = ProductionRunForm(request.POST or None, user=request.user)
    store = scoped(RawMaterial.objects.active(), request.user).order_by("name")

    if request.method == "POST" and form.is_valid():
        lines, errors = parse_material_lines(request, request.user)
        if errors:
            for error in errors:
                messages.error(request, error)
        else:
            try:
                run = record_production(
                    product=form.cleaned_data["product"],
                    quantity_produced=form.cleaned_data["quantity_produced"],
                    quantity_rejected=form.cleaned_data.get("quantity_rejected") or 0,
                    produced_on=form.cleaned_data.get("produced_on"),
                    notes=form.cleaned_data.get("notes", ""),
                    materials=lines,
                    user=request.user,
                )
            except (ValidationError, DatabaseError) as exc:
                for message in _messages_of(exc):
                    messages.error(request, message)
            else:
                log_action(
                    AuditAction.STOCK, instance=run,
                    description=(
                        f"Recorded {run.reference}: {run.quantity_produced} x "
                        f"{run.product.name}."
                    ),
                )
                messages.success(
                    request,
                    f"{run.reference} recorded. {run.quantity_produced} "
                    f"{run.product.name} added to stock.",
                )
                return redirect("production:run_detail", pk=run.pk)

    return render(
        request,
        "production/run_form.html",
        {"form": form, "store": store},
    )


def run_reverse(request, pk):
    blocked = require(
        request, "production.reverse",
        message="You do not have permission to reverse a production run.",
    )
    if blocked:
        return blocked

    run = get_owned_or_404(ProductionRun.objects.all(), request.user, pk=pk)
    form = ReversalForm(request.POST or None)

    if request.method == "POST" and form.is_valid():
        try:
            reverse_production(run, user=request.user,
                               reason=form.cleaned_data["reason"])
        except (ValidationError, DatabaseError) as exc:
            for message in _messages_of(exc):
                messages.error(request, message)
        else:
            log_action(
                AuditAction.STOCK, instance=run,
                description=f"Reversed {run.reference}.",
            )
            messages.success(request, f"{run.reference} reversed.")
        return redirect("production:run_detail", pk=run.pk)

    return redirect("production:run_detail", pk=run.pk)


# ---------------------------------------------------------------------------
# The planner - used by the run form as you type
# ---------------------------------------------------------------------------
def plan_api(request):
    """
    What a batch would need, before anything is written.

    Called by the run form when a product or a quantity changes, so the lines
    arrive pre-filled and any shortage is on screen while it can still be
    acted on.
    """
    blocked = require(request, "production.create")
    if blocked:
        return blocked

    try:
        product_id = int(request.GET.get("product", 0))
        quantity = int(request.GET.get("quantity", 0))
    except (TypeError, ValueError):
        return JsonResponse({"detail": "Give a product and a quantity."}, status=400)

    product = scoped(Product.objects.active(), request.user).filter(
        pk=product_id
    ).first()
    if product is None:
        return JsonResponse({"detail": "That product is not yours."}, status=404)

    try:
        plan = plan_for(product, quantity, user=request.user)
    except (ValidationError, DatabaseError) as exc:
        return JsonResponse(
            {"detail": describe(exc, context="production plan")},
            status=400 if isinstance(exc, ValidationError) else 500,
        )

    return JsonResponse(
        {
            "has_recipe": plan["has_recipe"],
            "output_quantity": plan["output_quantity"],
            "material_cost": str(plan["material_cost"]),
            "unit_cost": str(plan["unit_cost"]),
            "can_produce": plan["can_produce"],
            "shortages": plan["shortages"],
            "lines": [
                {
                    "material": line["material_id"],
                    "name": line["material_name"],
                    "code": line["material_code"],
                    "unit": line["unit_display"],
                    "required": str(line["required"]),
                    "available": str(line["available"]),
                    "is_short": line["is_short"],
                    "short_by": str(line["short_by"]),
                }
                for line in plan["lines"]
            ],
        }
    )
