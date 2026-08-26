from django.contrib import messages
from django.db.models import Q, Sum
from django.http import JsonResponse
from django.shortcuts import redirect, render
from django.urls import reverse_lazy
from django.views.generic import CreateView, DetailView, ListView, UpdateView

from accounts.models import AuditAction
from accounts.services import diff_instance, log_action
from core.mixins import (
    AdminRequiredMixin,
    AuthorStampMixin,
    OwnerScopedMixin,
    StaffRequiredMixin,
    get_owned_or_404,
)
from core.scoping import scoped

from .forms import (
    CategoryForm,
    ProductFilterForm,
    ProductForm,
    RestockForm,
    StockAdjustForm,
    SupplierForm,
)
from .models import Category, MovementType, Product, StockMovement, Supplier
from .services import adjust_to, restock, return_from_customer, write_off


# ---------------------------------------------------------------------------
# Products
# ---------------------------------------------------------------------------
class ProductListView(OwnerScopedMixin, StaffRequiredMixin, ListView):
    model = Product
    template_name = "inventory/product_list.html"
    context_object_name = "products"
    paginate_by = 25

    def get_queryset(self):
        qs = super().get_queryset().alive().select_related("category", "supplier", "owner")
        form = ProductFilterForm(self.request.GET or None)
        if form.is_valid():
            q = form.cleaned_data.get("q")
            category = form.cleaned_data.get("category")
            stock = form.cleaned_data.get("stock")
            status = form.cleaned_data.get("status")
            if q:
                qs = qs.filter(
                    Q(name__icontains=q) | Q(sku__icontains=q) | Q(barcode__icontains=q)
                )
            if category:
                qs = qs.filter(category=category)
            if stock == "low":
                qs = qs.low_stock()
            elif stock == "out":
                qs = qs.out_of_stock()
            elif stock == "ok":
                qs = qs.active().exclude(pk__in=qs.needs_attention().values("pk"))
            if status == "active":
                qs = qs.filter(is_active=True)
            elif status == "inactive":
                qs = qs.filter(is_active=False)
        self.filter_form = form
        return qs.order_by("name")

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        alive = scoped(Product.objects.alive(), self.request.user)
        ctx["filter_form"] = self.filter_form
        ctx["total_products"] = alive.count()
        ctx["low_stock_count"] = alive.low_stock().count()
        ctx["out_of_stock_count"] = alive.out_of_stock().count()
        if self.request.user.can_view_financials:
            ctx["total_stock_value"] = (
                alive.with_stock_value().aggregate(t=Sum("stock_value"))["t"] or 0
            )
        return ctx


class ProductDetailView(OwnerScopedMixin, StaffRequiredMixin, DetailView):
    model = Product
    template_name = "inventory/product_detail.html"
    context_object_name = "product"

    def get_queryset(self):
        return (
            super()
            .get_queryset()
            .alive()
            .select_related("category", "supplier", "created_by", "owner")
        )

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["movements"] = self.object.stock_movements.select_related("performed_by")[:50]
        ctx["restock_form"] = RestockForm(user=self.request.user)
        ctx["adjust_form"] = StockAdjustForm()
        ctx["recent_sales"] = self.object.sale_items.select_related(
            "transaction", "transaction__customer"
        ).order_by("-id")[:10]
        return ctx


class ProductCreateView(StaffRequiredMixin, AuthorStampMixin, CreateView):
    model = Product
    form_class = ProductForm
    template_name = "inventory/product_form.html"

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["user"] = self.request.user
        return kwargs

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["title"] = "Add Product"
        return ctx

    def form_valid(self, form):
        response = super().form_valid(form)
        log_action(
            AuditAction.CREATE,
            instance=self.object,
            description=f"Created product '{self.object.name}' (SKU {self.object.sku}).",
        )
        messages.success(self.request, f"Product '{self.object.name}' created.")
        _announce_new_lookups(self.request, form)
        return response


class ProductUpdateView(OwnerScopedMixin, StaffRequiredMixin, AuthorStampMixin, UpdateView):
    model = Product
    form_class = ProductForm
    template_name = "inventory/product_form.html"

    def get_queryset(self):
        return super().get_queryset().alive()

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["user"] = self.request.user
        return kwargs

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["title"] = f"Edit {self.object.name}"
        return ctx

    def form_valid(self, form):
        before = Product.objects.get(pk=self.object.pk)
        response = super().form_valid(form)
        changes = diff_instance(
            before,
            self.object,
            ["name", "sku", "barcode", "cost_price", "selling_price",
             "low_stock_threshold", "is_active"],
        )
        log_action(
            AuditAction.UPDATE,
            instance=self.object,
            description=f"Updated product '{self.object.name}'.",
            changes=changes,
        )
        messages.success(self.request, "Product updated.")
        _announce_new_lookups(self.request, form)
        return response


def product_delete(request, pk):
    """Soft delete. Admin only - the record and its history survive."""
    if not request.user.is_admin:
        messages.error(request, "Only an administrator may delete products.")
        return redirect("core:forbidden")

    product = get_owned_or_404(Product.objects.alive(), request.user, pk=pk)

    if request.method == "POST":
        product.soft_delete(user=request.user)
        log_action(
            AuditAction.DELETE,
            instance=product,
            description=f"Soft-deleted product '{product.name}' (SKU {product.sku}).",
        )
        messages.success(
            request,
            f"Product '{product.name}' archived. Sales history is preserved.",
        )
        return redirect("inventory:product_list")

    return render(request, "inventory/product_confirm_delete.html", {"product": product})


def _announce_new_lookups(request, form):
    """
    Category and Supplier are free-text fields, so a typo silently creates a
    new row. Telling the user what was created lets them spot 'Bevrages'
    before it becomes a permanent duplicate.
    """
    created = getattr(form, "created_lookups", None)
    if created:
        messages.info(
            request,
            "Created new " + " and ".join(created)
            + ". If that was a typo, edit it under Categories or Suppliers.",
        )
        for name in created:
            log_action(AuditAction.CREATE, description=f"Auto-created {name} from the product form.")


# ---------------------------------------------------------------------------
# Stock operations
# ---------------------------------------------------------------------------
def product_restock(request, pk):
    if not request.user.is_authenticated:
        return redirect("accounts:login")

    product = get_owned_or_404(Product.objects.alive(), request.user, pk=pk)
    form = RestockForm(request.POST or None, user=request.user)

    if request.method == "POST" and form.is_valid():
        movement = restock(
            product,
            form.cleaned_data["quantity"],
            user=request.user,
            unit_cost=form.cleaned_data.get("unit_cost"),
            reference=form.cleaned_data.get("reference", ""),
            reason=form.cleaned_data.get("reason", ""),
        )
        log_action(
            AuditAction.STOCK,
            instance=product,
            description=(
                f"Restocked '{product.name}' by {movement.quantity_delta:+d} "
                f"({movement.quantity_before} -> {movement.quantity_after})."
            ),
        )
        messages.success(
            request,
            f"Restocked {movement.quantity_delta} units. New level: {movement.quantity_after}.",
        )
        return redirect("inventory:product_detail", pk=product.pk)

    return render(
        request, "inventory/product_restock.html", {"form": form, "product": product}
    )


def product_adjust(request, pk):
    if not request.user.is_authenticated:
        return redirect("accounts:login")

    product = get_owned_or_404(Product.objects.alive(), request.user, pk=pk)
    form = StockAdjustForm(request.POST or None)

    if request.method == "POST" and form.is_valid():
        mode = form.cleaned_data["mode"]
        qty = form.cleaned_data["quantity"]
        reason = form.cleaned_data["reason"]

        # A stock-take that changes the count is a financial event - Admin only.
        if mode == "SET" and not request.user.is_admin:
            messages.error(
                request, "Only an administrator may overwrite a stock count."
            )
            return redirect("inventory:product_detail", pk=product.pk)

        if mode == "SET":
            movement = adjust_to(product, qty, user=request.user, reason=reason)
        elif mode == "DAMAGE":
            movement = write_off(product, qty, user=request.user, reason=reason)
        else:
            movement = return_from_customer(product, qty, user=request.user, reason=reason)

        if movement is None:
            messages.info(request, "No change - the counted quantity already matches.")
        else:
            log_action(
                AuditAction.STOCK,
                instance=product,
                description=(
                    f"{movement.get_movement_type_display()} on '{product.name}': "
                    f"{movement.quantity_delta:+d} ({movement.quantity_before} -> "
                    f"{movement.quantity_after}). Reason: {reason}"
                ),
            )
            messages.success(
                request, f"Stock adjusted. New level: {movement.quantity_after}."
            )
        return redirect("inventory:product_detail", pk=product.pk)

    return render(
        request, "inventory/product_adjust.html", {"form": form, "product": product}
    )


class StockMovementListView(OwnerScopedMixin, StaffRequiredMixin, ListView):
    model = StockMovement
    template_name = "inventory/stock_movement_list.html"
    context_object_name = "movements"
    paginate_by = 50

    def get_queryset(self):
        qs = super().get_queryset().select_related("product", "performed_by")
        mtype = self.request.GET.get("type", "").strip()
        product_id = self.request.GET.get("product", "").strip()
        q = self.request.GET.get("q", "").strip()
        if mtype:
            qs = qs.filter(movement_type=mtype)
        if product_id:
            qs = qs.filter(product_id=product_id)
        if q:
            qs = qs.filter(
                Q(product__name__icontains=q)
                | Q(product__sku__icontains=q)
                | Q(reference__icontains=q)
                | Q(reason__icontains=q)
            )
        return qs

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["movement_types"] = MovementType.choices
        ctx["selected_type"] = self.request.GET.get("type", "")
        ctx["q"] = self.request.GET.get("q", "")
        return ctx


class LowStockView(StaffRequiredMixin, ListView):
    template_name = "inventory/low_stock.html"
    context_object_name = "products"
    paginate_by = 50

    def get_queryset(self):
        return (
            scoped(Product.objects.alive(), self.request.user)
            .needs_attention()
            .select_related("category", "supplier")
            .order_by("stock_quantity", "name")
        )

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        alive = scoped(Product.objects.alive(), self.request.user)
        ctx["out_count"] = alive.out_of_stock().count()
        ctx["low_count"] = alive.low_stock().count()
        return ctx


# ---------------------------------------------------------------------------
# Categories & Suppliers
# ---------------------------------------------------------------------------
class CategoryListView(StaffRequiredMixin, ListView):
    model = Category
    template_name = "inventory/category_list.html"
    context_object_name = "categories"
    paginate_by = 30


class CategoryCreateView(StaffRequiredMixin, CreateView):
    model = Category
    form_class = CategoryForm
    template_name = "inventory/simple_form.html"
    success_url = reverse_lazy("inventory:category_list")

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["title"] = "Add Category"
        ctx["cancel_url"] = self.success_url
        return ctx

    def form_valid(self, form):
        response = super().form_valid(form)
        log_action(AuditAction.CREATE, instance=self.object,
                   description=f"Created category '{self.object.name}'.")
        messages.success(self.request, "Category created.")
        return response


class CategoryUpdateView(StaffRequiredMixin, UpdateView):
    model = Category
    form_class = CategoryForm
    template_name = "inventory/simple_form.html"
    success_url = reverse_lazy("inventory:category_list")

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["title"] = f"Edit {self.object.name}"
        ctx["cancel_url"] = self.success_url
        return ctx

    def form_valid(self, form):
        response = super().form_valid(form)
        log_action(AuditAction.UPDATE, instance=self.object,
                   description=f"Updated category '{self.object.name}'.")
        messages.success(self.request, "Category updated.")
        return response


class SupplierListView(StaffRequiredMixin, ListView):
    model = Supplier
    template_name = "inventory/supplier_list.html"
    context_object_name = "suppliers"
    paginate_by = 30

    def get_queryset(self):
        qs = Supplier.objects.all()
        q = self.request.GET.get("q", "").strip()
        if q:
            qs = qs.filter(
                Q(name__icontains=q) | Q(phone__icontains=q) | Q(contact_person__icontains=q)
            )
        return qs


class SupplierCreateView(StaffRequiredMixin, CreateView):
    model = Supplier
    form_class = SupplierForm
    template_name = "inventory/simple_form.html"
    success_url = reverse_lazy("inventory:supplier_list")

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["title"] = "Add Supplier"
        ctx["cancel_url"] = self.success_url
        return ctx

    def form_valid(self, form):
        response = super().form_valid(form)
        log_action(AuditAction.CREATE, instance=self.object,
                   description=f"Created supplier '{self.object.name}'.")
        messages.success(self.request, "Supplier created.")
        return response


class SupplierUpdateView(StaffRequiredMixin, UpdateView):
    model = Supplier
    form_class = SupplierForm
    template_name = "inventory/simple_form.html"
    success_url = reverse_lazy("inventory:supplier_list")

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["title"] = f"Edit {self.object.name}"
        ctx["cancel_url"] = self.success_url
        return ctx


# ---------------------------------------------------------------------------
# AJAX - product lookup for the point-of-sale screen
# ---------------------------------------------------------------------------
def product_search_api(request):
    """Typeahead/barcode lookup used by the sale entry screen."""
    if not request.user.is_authenticated:
        return JsonResponse({"results": []}, status=403)

    term = request.GET.get("q", "").strip()
    if len(term) < 1:
        return JsonResponse({"results": []})

    products = (
        scoped(Product.objects.active(), request.user)
        .filter(Q(name__icontains=term) | Q(sku__icontains=term) | Q(barcode__iexact=term))
        .select_related("category")[:20]
    )

    show_cost = request.user.can_view_financials
    return JsonResponse(
        {
            "results": [
                {
                    "id": p.pk,
                    "name": p.name,
                    "sku": p.sku,
                    "barcode": p.barcode or "",
                    "unit": p.get_unit_display(),
                    "selling_price": str(p.selling_price),
                    "cost_price": str(p.cost_price) if show_cost else None,
                    "stock": p.stock_quantity,
                    "status": p.stock_status,
                    "category": p.category.name if p.category else "",
                }
                for p in products
            ]
        }
    )
