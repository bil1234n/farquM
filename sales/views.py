from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.db.models import Count, Q, Sum
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
from core.scoping import scoped, sees_everything, visible_users
from core.utils import ZERO, money
from inventory.models import Product

from .forms import (
    CustomerForm,
    ReceiptUploadForm,
    SaleHeaderForm,
    TransactionFilterForm,
    VoidTransactionForm,
)
from .models import Customer, PaymentStatus, Receipt, Transaction
from .services import SaleError, create_sale, void_transaction


# ---------------------------------------------------------------------------
# Customers
# ---------------------------------------------------------------------------
class CustomerListView(OwnerScopedMixin, PermissionRequiredMixin, ListView):
    required_permission = "customer.view"
    model = Customer
    template_name = "sales/customer_list.html"
    context_object_name = "customers"
    paginate_by = 25

    def get_queryset(self):
        qs = super().get_queryset().select_related("credit_account", "owner")
        q = self.request.GET.get("q", "").strip()
        flt = self.request.GET.get("filter", "").strip()
        if q:
            qs = qs.filter(
                Q(name__icontains=q) | Q(phone__icontains=q)
                | Q(alternate_phone__icontains=q) | Q(email__icontains=q)
            )
        if flt == "debtors":
            qs = qs.filter(credit_account__outstanding_balance__gt=0)
        elif flt == "credit":
            qs = qs.filter(is_credit_approved=True)
        elif flt == "inactive":
            qs = qs.filter(is_active=False)
        return qs.order_by("name")

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        mine = scoped(Customer.objects.all(), self.request.user)
        ctx["q"] = self.request.GET.get("q", "")
        ctx["selected_filter"] = self.request.GET.get("filter", "")
        ctx["total_customers"] = mine.count()
        ctx["debtor_count"] = mine.with_debt().count()
        return ctx


class CustomerDetailView(OwnerScopedMixin, PermissionRequiredMixin, DetailView):
    required_permission = "customer.view"
    model = Customer
    template_name = "sales/customer_detail.html"
    context_object_name = "customer"

    def get_queryset(self):
        return super().get_queryset().select_related("credit_account", "owner")

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["transactions"] = (
            self.object.transactions.active().order_by("-created_at")[:25]
        )
        ctx["debts"] = (
            self.object.debts.select_related("transaction").order_by("-created_at")[:25]
            if hasattr(self.object, "debts") else []
        )
        ctx["account"] = getattr(self.object, "credit_account", None)
        return ctx


class CustomerCreateView(PermissionRequiredMixin, AuthorStampMixin, CreateView):
    required_permission = "customer.create"
    model = Customer
    form_class = CustomerForm
    template_name = "sales/customer_form.html"

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["user"] = self.request.user
        return kwargs

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["title"] = "Add Customer"
        return ctx

    def form_valid(self, form):
        response = super().form_valid(form)
        log_action(
            AuditAction.CREATE, instance=self.object,
            description=f"Registered customer '{self.object.name}' ({self.object.phone}).",
        )
        messages.success(self.request, f"Customer '{self.object.name}' registered.")
        return response


class CustomerUpdateView(OwnerScopedMixin, PermissionRequiredMixin, AuthorStampMixin, UpdateView):
    required_permission = "customer.edit"
    model = Customer
    form_class = CustomerForm
    template_name = "sales/customer_form.html"

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["user"] = self.request.user
        return kwargs

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["title"] = f"Edit {self.object.name}"
        return ctx

    def form_valid(self, form):
        response = super().form_valid(form)
        log_action(
            AuditAction.UPDATE, instance=self.object,
            description=f"Updated customer '{self.object.name}'.",
        )
        messages.success(self.request, "Customer updated.")
        return response


# ---------------------------------------------------------------------------
# Transactions
# ---------------------------------------------------------------------------
class TransactionListView(OwnerScopedMixin, PermissionRequiredMixin, ListView):
    required_permission = "sale.view"
    model = Transaction
    template_name = "sales/transaction_list.html"
    context_object_name = "transactions"
    paginate_by = 25

    def get_queryset(self):
        qs = (
            super()
            .get_queryset()
            .select_related("customer", "sold_by", "owner")
            .annotate(line_count=Count("items"))
        )
        form = TransactionFilterForm(
            self.request.GET or None, sellers=visible_users(self.request.user)
        )
        if form.is_valid():
            q = form.cleaned_data.get("q")
            status = form.cleaned_data.get("status")
            date_from = form.cleaned_data.get("date_from")
            date_to = form.cleaned_data.get("date_to")
            seller = form.cleaned_data.get("seller")
            if q:
                qs = qs.filter(
                    Q(reference__icontains=q)
                    | Q(customer__name__icontains=q)
                    | Q(customer__phone__icontains=q)
                    | Q(customer_name_snapshot__icontains=q)
                )
            if status == "REFUNDED":
                qs = qs.filter(is_voided=True)
            elif status:
                qs = qs.filter(payment_status=status, is_voided=False)
            if date_from:
                qs = qs.filter(created_at__date__gte=date_from)
            if date_to:
                qs = qs.filter(created_at__date__lte=date_to)
            if seller:
                qs = qs.filter(sold_by_id=seller)
        self.filter_form = form
        return qs.order_by("-created_at")

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        page_qs = self.get_queryset()
        totals = page_qs.aggregate(
            revenue=Sum("total_amount"), collected=Sum("amount_paid"),
            outstanding=Sum("balance_due"),
        )
        ctx["filter_form"] = self.filter_form
        ctx["sum_revenue"] = totals["revenue"] or ZERO
        ctx["sum_collected"] = totals["collected"] or ZERO
        ctx["sum_outstanding"] = totals["outstanding"] or ZERO
        ctx["result_count"] = page_qs.count()
        ctx["show_seller_column"] = sees_everything(self.request.user)
        return ctx


class TransactionDetailView(OwnerScopedMixin, PermissionRequiredMixin, DetailView):
    required_permission = "sale.view"
    model = Transaction
    template_name = "sales/transaction_detail.html"
    context_object_name = "txn"

    def get_queryset(self):
        return (
            super()
            .get_queryset()
            .select_related("customer", "sold_by", "voided_by", "owner")
            .prefetch_related("items", "receipts")
        )

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["receipt_form"] = ReceiptUploadForm()
        ctx["debt"] = getattr(self.object, "debt_record", None)
        return ctx


def sale_create(request):
    """
    Point-of-sale screen.

    Cart lines arrive as parallel arrays:
        product_id[]  quantity[]  unit_price[]  line_discount[]
    """
    blocked = require(
        request, "sale.create",
        message="You do not have permission to record sales.",
    )
    if blocked:
        return blocked

    can_credit = request.user.has_access("sale.credit")
    can_discount = request.user.has_access("sale.discount")
    form = SaleHeaderForm(
        request.POST or None,
        request.FILES or None,
        user=request.user,
        can_credit=can_credit,
        can_discount=can_discount,
    )

    if request.method == "POST":
        cart, cart_errors = _parse_cart(request)

        if cart_errors:
            for err in cart_errors:
                messages.error(request, err)
        elif form.is_valid():
            try:
                txn = create_sale(
                    user=request.user,
                    cart=cart,
                    customer=form.cleaned_data.get("customer"),
                    amount_paid=form.cleaned_data.get("amount_paid") or ZERO,
                    discount_amount=form.cleaned_data.get("discount_amount") or ZERO,
                    tax_amount=form.cleaned_data.get("tax_amount") or ZERO,
                    payment_method=form.cleaned_data["payment_method"],
                    due_date=form.cleaned_data.get("due_date"),
                    notes=form.cleaned_data.get("notes", ""),
                )
            except (SaleError, ValidationError) as exc:
                for msg in getattr(exc, "messages", [str(exc)]):
                    messages.error(request, msg)
            else:
                attached = _attach_sale_receipts(request, txn, form)

                log_action(
                    AuditAction.CREATE, instance=txn,
                    description=(
                        f"Recorded sale {txn.reference} for {txn.customer_display}: "
                        f"total {txn.total_amount}, paid {txn.amount_paid}, "
                        f"balance {txn.balance_due}."
                        + (f" {attached} receipt(s) attached." if attached else "")
                    ),
                )
                if txn.balance_due > ZERO:
                    messages.warning(
                        request,
                        f"Sale {txn.reference} saved with an outstanding balance of "
                        f"{txn.balance_due}. A debt record has been opened.",
                    )
                else:
                    messages.success(request, f"Sale {txn.reference} completed.")

                if attached:
                    messages.info(
                        request,
                        f"{attached} receipt{'s' if attached != 1 else ''} attached to {txn.reference}.",
                    )
                warning = getattr(form, "proof_warning", None)
                if warning:
                    messages.warning(request, warning)

                return redirect("sales:transaction_detail", pk=txn.pk)

    return render(
        request,
        "sales/sale_create.html",
        {
            "form": form,
            "products": scoped(Product.objects.active(), request.user)
            .select_related("category")[:200],
            "customers": (
                scoped(Customer.objects.active(), request.user)
                .select_related("credit_account")
                .order_by("name")
            ),
            "show_cost": request.user.can_view_costs,
            "can_credit": can_credit,
            "can_discount": can_discount,
        },
    )


def _attach_sale_receipts(request, txn, form):
    """
    Save any files uploaded on the point-of-sale screen against the new sale.

    Deliberately runs AFTER create_sale() and outside its atomic block: a
    corrupt upload must never roll back a completed sale and un-deduct stock.
    A missing receipt is recoverable from the transaction page; a phantom sale
    is not.
    """
    files = form.cleaned_data.get("receipt") or []
    if not files:
        return 0

    kind = form.cleaned_data.get("receipt_kind") or Receipt.Kind.SALE
    saved = 0
    for f in files:
        try:
            Receipt.objects.create(
                transaction=txn, file=f, kind=kind,
                caption=f"Captured at point of sale", uploaded_by=request.user,
            )
            saved += 1
        except ValidationError as exc:
            for msg in getattr(exc, "messages", [str(exc)]):
                messages.error(request, f"Receipt '{f.name}' was not saved: {msg}")
        except Exception:
            messages.error(
                request,
                f"Receipt '{f.name}' could not be saved. The sale itself was "
                "recorded correctly - attach the file again from the transaction page.",
            )
    return saved


def _parse_cart(request):
    """Turn the POSTed parallel arrays into a validated cart list."""
    may_discount = request.user.has_access("sale.discount")
    product_ids = request.POST.getlist("product_id[]")
    quantities = request.POST.getlist("quantity[]")
    prices = request.POST.getlist("unit_price[]")
    discounts = request.POST.getlist("line_discount[]")

    cart, errors = [], []
    if not product_ids:
        return cart, ["Add at least one product to the sale."]

    # Scoped, so a hand-edited form naming another manager's product ID lands
    # in the "not found" branch below rather than selling their stock.
    products = {
        p.pk: p
        for p in scoped(Product.objects.active(), request.user).filter(
            pk__in=[i for i in product_ids if i]
        )
    }

    for idx, pid in enumerate(product_ids):
        if not pid:
            continue
        try:
            product = products[int(pid)]
        except (KeyError, ValueError):
            errors.append(f"Line {idx + 1}: product not found or inactive.")
            continue
        try:
            qty = int(quantities[idx])
            price = money(Decimal(prices[idx] or "0"))
            disc = money(Decimal(discounts[idx] or "0")) if idx < len(discounts) else ZERO
            # A per-line discount is still a discount. Dropped here rather
            # than rejected so the cart is not lost over a hidden input the
            # user never saw; create_sale refuses it as well if it survives.
            if not may_discount:
                disc = ZERO
        except (IndexError, ValueError, InvalidOperation):
            errors.append(f"Line {idx + 1}: invalid quantity or price.")
            continue
        if qty <= 0:
            errors.append(f"Line {idx + 1}: quantity must be at least 1.")
            continue
        cart.append(
            {"product": product, "quantity": qty, "unit_price": price, "line_discount": disc}
        )

    if not cart and not errors:
        errors.append("Add at least one product to the sale.")
    return cart, errors


def transaction_void(request, pk):
    """Reverses stock and cancels any linked debt. Its own permission."""
    blocked = require(
        request, "sale.void",
        message="You do not have permission to void a sale.",
    )
    if blocked:
        return blocked

    txn = get_owned_or_404(Transaction, request.user, pk=pk)
    if txn.is_voided:
        messages.info(request, "This transaction is already voided.")
        return redirect("sales:transaction_detail", pk=txn.pk)

    form = VoidTransactionForm(request.POST or None)

    if request.method == "POST" and form.is_valid():
        try:
            void_transaction(txn, user=request.user, reason=form.cleaned_data["reason"])
        except (SaleError, ValidationError) as exc:
            for msg in getattr(exc, "messages", [str(exc)]):
                messages.error(request, msg)
        else:
            log_action(
                AuditAction.VOID, instance=txn,
                description=(
                    f"VOIDED sale {txn.reference}. Stock reversed, debt cancelled. "
                    f"Reason: {form.cleaned_data['reason']}"
                ),
            )
            messages.success(
                request, f"{txn.reference} voided. Stock has been returned."
            )
            return redirect("sales:transaction_detail", pk=txn.pk)

    return render(request, "sales/transaction_void.html", {"form": form, "txn": txn})


def receipt_upload(request, pk):
    blocked = require(
        request, "sale.receipt.add",
        message="You do not have permission to attach receipts.",
    )
    if blocked:
        return blocked

    txn = get_owned_or_404(Transaction, request.user, pk=pk)
    form = ReceiptUploadForm(request.POST or None, request.FILES or None)

    if request.method == "POST" and form.is_valid():
        receipt = form.save(commit=False)
        receipt.transaction = txn
        receipt.uploaded_by = request.user
        receipt.save()
        log_action(
            AuditAction.CREATE, instance=receipt,
            description=f"Uploaded {receipt.get_kind_display()} for {txn.reference}.",
        )
        messages.success(request, "Receipt attached.")
        return redirect("sales:transaction_detail", pk=txn.pk)

    return render(request, "sales/receipt_upload.html", {"form": form, "txn": txn})


def receipt_delete(request, pk):
    """Removing proof of a payment is a sensitive action with its own key."""
    blocked = require(
        request, "sale.receipt.delete",
        message="You do not have permission to remove a receipt.",
    )
    if blocked:
        return blocked

    receipt = get_owned_or_404(
        Receipt.objects.select_related("transaction"), request.user, pk=pk
    )
    txn_pk = receipt.transaction.pk

    if request.method == "POST":
        log_action(
            AuditAction.DELETE, instance=receipt,
            description=f"Deleted receipt '{receipt.filename}' from {receipt.transaction.reference}.",
        )
        receipt.file.delete(save=False)
        receipt.delete()
        messages.success(request, "Receipt removed.")
        return redirect("sales:transaction_detail", pk=txn_pk)

    return render(request, "sales/receipt_confirm_delete.html", {"receipt": receipt})


def transaction_print(request, pk):
    blocked = require(request, "sale.view")
    if blocked:
        return blocked
    txn = get_owned_or_404(
        Transaction.objects.select_related("customer", "sold_by").prefetch_related("items"),
        request.user,
        pk=pk,
    )
    return render(request, "sales/transaction_print.html", {"txn": txn})
