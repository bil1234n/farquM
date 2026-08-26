"""
API views.

Every write goes through the same service layer the web UI uses
(sales.services, credit.services, inventory.services). The API is a transport,
not a second implementation. If the rules ever diverge, one of the two clients
is wrong and nobody will notice until the books don't balance.

DATA ISOLATION
--------------
Every queryset below is wrapped in core.scoping.scoped(). A Manager sees only
their own products, customers, sales and debts; an Admin sees everyone's.
Because get_queryset() is scoped, get_object() is scoped too - so a Manager
who guesses another manager's transaction ID gets a 404, not a 403. A 403
would confirm the record exists, which is itself a small leak.
"""
import datetime as dt
import logging

from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.db.models import Count, Q
from django.utils import timezone
from rest_framework import status, viewsets
from rest_framework.authtoken.models import Token
from rest_framework.decorators import (
    action,
    api_view,
    parser_classes,
    permission_classes,
)
from rest_framework.pagination import PageNumberPagination
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.models import AuditAction, User
from accounts.services import log_action
from core.scoping import scoped, sees_everything
from credit.models import CreditAccount, DebtRecord, Repayment
from credit.services import (
    CreditError,
    aging_summary,
    bulk_settle_customer,
    record_repayment,
    write_off_debt,
)
from inventory.models import Category, Product, StockMovement, Supplier
from inventory.services import restock as do_restock
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
from sales.models import Customer, Receipt, Transaction
from sales.services import SaleError, create_sale, void_transaction

from .models import DeviceToken, NotificationLog
from .permissions import IsAdmin, IsStaff, ReadOnlyOrAdmin, StaffWriteAdminDelete
from .serializers import (
    CategorySerializer,
    CustomerSerializer,
    DebtSerializer,
    DeviceTokenSerializer,
    LoginSerializer,
    NotificationSerializer,
    ProductSerializer,
    RepaymentCreateSerializer,
    RepaymentSerializer,
    RestockSerializer,
    SaleCreateSerializer,
    StockMovementSerializer,
    SupplierSerializer,
    TransactionSerializer,
    UserAdminSerializer,
    UserSerializer,
)

logger = logging.getLogger(__name__)


class StandardPagination(PageNumberPagination):
    page_size = 25
    page_size_query_param = "page_size"
    max_page_size = 200


def _error(exc, default="Something went wrong."):
    msgs = getattr(exc, "messages", None) or [str(exc) or default]
    return Response({"detail": msgs[0], "errors": msgs}, status=status.HTTP_400_BAD_REQUEST)


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------
class LoginView(APIView):
    permission_classes = []
    authentication_classes = []

    def post(self, request):
        serializer = LoginSerializer(data=request.data, context={"request": request})
        if not serializer.is_valid():
            log_action(
                AuditAction.LOGIN_FAILED,
                description=f"Failed API login for '{request.data.get('username','')[:150]}'.",
                user=None, request=request,
            )
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        user = serializer.validated_data["user"]
        token, _ = Token.objects.get_or_create(user=user)
        user.touch()

        log_action(
            AuditAction.LOGIN, instance=user,
            description=f"{user.username} signed in from the mobile app.",
            user=user, request=request,
        )
        return Response({
            "token": token.key,
            "user": UserSerializer(user, context={"request": request}).data,
        })


class LogoutView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        """Drop the token and this device's push registration."""
        device_token = request.data.get("device_token")
        if device_token:
            DeviceToken.objects.filter(token=device_token, user=request.user).update(
                is_active=False, deactivated_reason="signed out"
            )
        Token.objects.filter(user=request.user).delete()
        log_action(
            AuditAction.LOGOUT, instance=request.user,
            description=f"{request.user.username} signed out of the mobile app.",
            user=request.user, request=request,
        )
        return Response({"detail": "Signed out."})


@api_view(["GET", "PATCH"])
@permission_classes([IsAuthenticated])
@parser_classes([MultiPartParser, FormParser, JSONParser])
def me(request):
    """
    The signed-in user's own profile.

    MultiPartParser is listed first because the profile photo is uploaded
    through this endpoint. Without a multipart parser DRF would reject the
    request with "Unsupported media type" and the avatar would never arrive -
    the API equivalent of a form missing its enctype.
    """
    if request.method == "PATCH":
        serializer = UserSerializer(
            request.user, data=request.data, partial=True, context={"request": request}
        )
        serializer.is_valid(raise_exception=True)
        user = serializer.save()
        log_action(
            AuditAction.UPDATE,
            instance=user,
            description="Updated own profile from the mobile app.",
            user=user,
            request=request,
        )
        return Response(UserSerializer(user, context={"request": request}).data)
    return Response(UserSerializer(request.user, context={"request": request}).data)


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def change_password(request):
    """Change your own password. Requires the current one."""
    current = request.data.get("current_password") or ""
    new = request.data.get("new_password") or ""

    if not request.user.check_password(current):
        return Response(
            {"detail": "Your current password is not correct."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    try:
        validate_password(new, user=request.user)
    except ValidationError as exc:
        return Response(
            {"detail": exc.messages[0], "errors": exc.messages},
            status=status.HTTP_400_BAD_REQUEST,
        )

    request.user.set_password(new)
    request.user.must_change_password = False
    request.user.save(update_fields=["password", "must_change_password"])

    # The old token was issued against the old credentials. Rotating it means
    # a stolen token stops working the moment the user changes their password,
    # which is the main reason people change it in the first place.
    Token.objects.filter(user=request.user).delete()
    token, _ = Token.objects.get_or_create(user=request.user)

    log_action(
        AuditAction.UPDATE,
        instance=request.user,
        description="Changed own password from the mobile app.",
        user=request.user,
        request=request,
    )
    return Response({"detail": "Password changed.", "token": token.key})


# ---------------------------------------------------------------------------
# User management - ADMIN ONLY
# ---------------------------------------------------------------------------
class UserViewSet(viewsets.ModelViewSet):
    """
    Staff administration from the phone.

    Admin-only in full. Note there is no destroy(): deleting a user would
    orphan every product, sale and debt they own, and `owner` is SET_NULL -
    so the records would survive but become invisible to everyone except an
    admin. Deactivation keeps the history intact and reversible.
    """

    serializer_class = UserAdminSerializer
    permission_classes = [IsAdmin]
    pagination_class = StandardPagination
    parser_classes = [MultiPartParser, FormParser, JSONParser]
    http_method_names = ["get", "post", "patch", "put", "head", "options"]

    def get_queryset(self):
        qs = User.objects.all().annotate(
            sales_count=Count("sales_transaction_owned", distinct=True)
        )
        params = self.request.query_params
        q = params.get("q", "").strip()
        if q:
            qs = qs.filter(
                Q(username__icontains=q)
                | Q(first_name__icontains=q)
                | Q(last_name__icontains=q)
                | Q(email__icontains=q)
                | Q(phone__icontains=q)
            )
        if params.get("role"):
            qs = qs.filter(role=params["role"])
        state = params.get("status", "")
        if state == "active":
            qs = qs.filter(is_active=True)
        elif state == "inactive":
            qs = qs.filter(is_active=False)
        return qs.order_by("-is_active", "role", "username")

    def perform_create(self, serializer):
        user = serializer.save()
        log_action(
            AuditAction.CREATE,
            instance=user,
            description=(
                f"Created {user.get_role_display()} account '{user.username}' "
                f"from the mobile app."
            ),
        )

    def perform_update(self, serializer):
        before = User.objects.get(pk=serializer.instance.pk)
        user = serializer.save()
        log_action(
            AuditAction.UPDATE,
            instance=user,
            description=(
                f"Updated account '{user.username}' from the mobile app "
                f"(role {before.role} -> {user.role}, "
                f"active {before.is_active} -> {user.is_active})."
            ),
        )

    @action(detail=True, methods=["post"])
    def toggle_active(self, request, pk=None):
        target = self.get_object()

        if target.pk == request.user.pk:
            return Response(
                {"detail": "You cannot deactivate your own account."}, status=400
            )
        if (
            target.is_active
            and target.is_admin
            and not User.objects.admins()
            .filter(is_active=True)
            .exclude(pk=target.pk)
            .exists()
        ):
            return Response(
                {"detail": "Cannot deactivate the only remaining administrator."},
                status=400,
            )

        target.is_active = not target.is_active
        target.save(update_fields=["is_active"])
        state = "reactivated" if target.is_active else "deactivated"
        log_action(
            AuditAction.UPDATE,
            instance=target,
            description=f"Account '{target.username}' {state} from the mobile app.",
        )
        return Response(
            UserAdminSerializer(target, context={"request": request}).data
        )

    @action(detail=True, methods=["post"])
    def reset_password(self, request, pk=None):
        target = self.get_object()
        new = request.data.get("new_password") or ""
        try:
            validate_password(new, user=target)
        except ValidationError as exc:
            return Response(
                {"detail": exc.messages[0], "errors": exc.messages}, status=400
            )

        target.set_password(new)
        target.must_change_password = True
        target.save(update_fields=["password", "must_change_password"])
        # Any session or token the user already had is now invalid, which is
        # the point: a password reset usually means the old one is compromised.
        Token.objects.filter(user=target).delete()

        log_action(
            AuditAction.OVERRIDE,
            instance=target,
            description=(
                f"Administrator reset the password for '{target.username}' "
                f"from the mobile app."
            ),
        )
        return Response({"detail": f"Password reset for {target.display_name}."})


# ---------------------------------------------------------------------------
# Devices
# ---------------------------------------------------------------------------
class RegisterDeviceView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        serializer = DeviceTokenSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        token_value = serializer.validated_data["token"]

        # A token belongs to an install, not a person. If someone else logs in
        # on the same handset, the token must follow the new user or the old
        # user keeps receiving that phone's notifications.
        device, _ = DeviceToken.objects.update_or_create(
            token=token_value,
            defaults={
                "user": request.user,
                "platform": serializer.validated_data.get("platform", "ANDROID"),
                "device_name": serializer.validated_data.get("device_name", ""),
                "app_version": serializer.validated_data.get("app_version", ""),
                "is_active": True,
                "last_seen": timezone.now(),
                "deactivated_reason": "",
            },
        )
        return Response(DeviceTokenSerializer(device).data, status=status.HTTP_201_CREATED)


class NotificationViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class = NotificationSerializer
    permission_classes = [IsStaff]
    pagination_class = StandardPagination

    def get_queryset(self):
        # Already per-user by construction - a notification is addressed to
        # exactly one person - so no extra scoping is needed here.
        return NotificationLog.objects.filter(user=self.request.user)

    @action(detail=True, methods=["post"])
    def read(self, request, pk=None):
        entry = self.get_object()
        entry.mark_read()
        return Response(self.get_serializer(entry).data)

    @action(detail=False, methods=["post"])
    def read_all(self, request):
        count = self.get_queryset().filter(read_at__isnull=True).update(
            read_at=timezone.now()
        )
        return Response({"marked": count})

    @action(detail=False, methods=["get"])
    def unread_count(self, request):
        return Response({"count": self.get_queryset().filter(read_at__isnull=True).count()})


# ---------------------------------------------------------------------------
# Inventory
# ---------------------------------------------------------------------------
class CategoryViewSet(viewsets.ModelViewSet):
    """
    Shared lookup list, deliberately NOT owner-scoped.

    A category is a label, not a business record. Giving every manager a
    private copy of "Furniture" would make reports incomparable and the
    dropdown baffling, and it exposes nothing: knowing a category exists
    tells you nothing about anyone's stock or takings.
    """

    serializer_class = CategorySerializer
    permission_classes = [StaffWriteAdminDelete]
    queryset = Category.objects.filter(is_active=True)
    pagination_class = None


class SupplierViewSet(viewsets.ModelViewSet):
    """Shared lookup list - see CategoryViewSet."""

    serializer_class = SupplierSerializer
    permission_classes = [StaffWriteAdminDelete]
    queryset = Supplier.objects.filter(is_active=True)
    pagination_class = None


class ProductViewSet(viewsets.ModelViewSet):
    serializer_class = ProductSerializer
    permission_classes = [StaffWriteAdminDelete]
    pagination_class = StandardPagination

    def get_queryset(self):
        qs = scoped(
            Product.objects.alive().select_related("category", "supplier", "owner"),
            self.request.user,
        )
        params = self.request.query_params

        q = params.get("q", "").strip()
        if q:
            qs = qs.filter(Q(name__icontains=q) | Q(sku__icontains=q) | Q(barcode__icontains=q))
        if params.get("category"):
            qs = qs.filter(category_id=params["category"])

        stock = params.get("stock", "")
        if stock == "low":
            qs = qs.low_stock()
        elif stock == "out":
            qs = qs.out_of_stock()
        elif stock == "attention":
            qs = qs.needs_attention()

        if params.get("active") == "true":
            qs = qs.filter(is_active=True)
        return qs.order_by("name")

    def perform_create(self, serializer):
        obj = serializer.save(
            owner=self.request.user,
            created_by=self.request.user,
            updated_by=self.request.user,
        )
        log_action(AuditAction.CREATE, instance=obj,
                   description=f"Created product '{obj.name}' from the mobile app.")

    def perform_update(self, serializer):
        # owner is never taken from the payload - reassignment is not a thing
        # a client gets to do by editing a field.
        obj = serializer.save(updated_by=self.request.user)
        log_action(AuditAction.UPDATE, instance=obj,
                   description=f"Updated product '{obj.name}' from the mobile app.")

    def perform_destroy(self, instance):
        instance.soft_delete(user=self.request.user)
        log_action(AuditAction.DELETE, instance=instance,
                   description=f"Archived product '{instance.name}' from the mobile app.")

    @action(detail=False, methods=["get"])
    def barcode(self, request):
        """Barcode scanner lookup. Returns one product or 404."""
        code = request.query_params.get("code", "").strip()
        if not code:
            return Response({"detail": "No barcode supplied."}, status=400)
        product = (
            self.get_queryset()
            .filter(is_active=True)
            .filter(Q(barcode__iexact=code) | Q(sku__iexact=code))
            .first()
        )
        if product is None:
            return Response({"detail": "No product with that barcode."}, status=404)
        return Response(self.get_serializer(product).data)

    @action(detail=False, methods=["get"])
    def low_stock(self, request):
        qs = self.get_queryset().needs_attention().order_by("stock_quantity")
        page = self.paginate_queryset(qs)
        return self.get_paginated_response(self.get_serializer(page, many=True).data)

    @action(detail=True, methods=["post"])
    def restock(self, request, pk=None):
        product = self.get_object()
        serializer = RestockSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        unit_cost = serializer.validated_data.get("unit_cost")
        if unit_cost is not None and not request.user.can_view_financials:
            unit_cost = None  # a Manager may not set cost prices

        try:
            movement = do_restock(
                product, serializer.validated_data["quantity"],
                user=request.user, unit_cost=unit_cost,
                reference=serializer.validated_data.get("reference", ""),
                reason=serializer.validated_data.get("reason", ""),
            )
        except ValidationError as exc:
            return _error(exc)

        log_action(AuditAction.STOCK, instance=product,
                   description=(f"Restocked '{product.name}' by {movement.quantity_delta:+d} "
                                f"from the mobile app."))
        return Response(StockMovementSerializer(movement).data, status=201)

    @action(detail=True, methods=["get"])
    def movements(self, request, pk=None):
        qs = self.get_object().stock_movements.select_related("performed_by")[:100]
        return Response(StockMovementSerializer(qs, many=True).data)


class StockMovementViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class = StockMovementSerializer
    permission_classes = [IsStaff]
    pagination_class = StandardPagination

    def get_queryset(self):
        qs = scoped(
            StockMovement.objects.select_related("product", "performed_by"),
            self.request.user,
        )
        mtype = self.request.query_params.get("type")
        if mtype:
            qs = qs.filter(movement_type=mtype)
        return qs


# ---------------------------------------------------------------------------
# Customers
# ---------------------------------------------------------------------------
class CustomerViewSet(viewsets.ModelViewSet):
    serializer_class = CustomerSerializer
    permission_classes = [StaffWriteAdminDelete]
    pagination_class = StandardPagination

    def get_queryset(self):
        qs = scoped(
            Customer.objects.select_related("credit_account", "owner"),
            self.request.user,
        )
        params = self.request.query_params
        q = params.get("q", "").strip()
        if q:
            qs = qs.filter(
                Q(name__icontains=q) | Q(phone__icontains=q)
                | Q(alternate_phone__icontains=q) | Q(email__icontains=q)
            )
        if params.get("filter") == "debtors":
            qs = qs.filter(credit_account__outstanding_balance__gt=0)
        elif params.get("filter") == "credit":
            qs = qs.filter(is_credit_approved=True)
        return qs.order_by("name")

    def perform_create(self, serializer):
        obj = serializer.save(
            owner=self.request.user,
            created_by=self.request.user,
            updated_by=self.request.user,
        )
        log_action(AuditAction.CREATE, instance=obj,
                   description=f"Registered customer '{obj.name}' from the mobile app.")

    def perform_update(self, serializer):
        serializer.save(updated_by=self.request.user)

    @action(detail=True, methods=["get"])
    def debts(self, request, pk=None):
        qs = self.get_object().debts.select_related("transaction").order_by("-created_at")
        return Response(DebtSerializer(qs, many=True).data)

    @action(detail=True, methods=["post"])
    def pay(self, request, pk=None):
        """Lump sum across all this customer's open debts, oldest first."""
        customer = self.get_object()
        serializer = RepaymentCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            receipts = bulk_settle_customer(
                customer=customer,
                amount=serializer.validated_data["amount"],
                user=request.user,
                method=serializer.validated_data.get("method", "CASH"),
                external_reference=serializer.validated_data.get("external_reference", ""),
                proof_files=request.FILES.getlist("proof"),
            )
        except (CreditError, ValidationError) as exc:
            return _error(exc)

        account = CreditAccount.objects.get(customer=customer)
        log_action(AuditAction.PAYMENT, instance=customer,
                   description=(f"Lump-sum payment of {serializer.validated_data['amount']} "
                                f"from {customer.name} via the mobile app."))
        return Response({
            "applied_to": len(receipts),
            "outstanding_balance": str(account.outstanding_balance),
            "repayments": RepaymentSerializer(receipts, many=True).data,
        }, status=201)


# ---------------------------------------------------------------------------
# Sales
# ---------------------------------------------------------------------------
class TransactionViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class = TransactionSerializer
    permission_classes = [IsStaff]
    pagination_class = StandardPagination

    def get_queryset(self):
        qs = scoped(
            Transaction.objects.select_related("customer", "sold_by", "owner")
            .prefetch_related("items", "receipts")
            .annotate(line_count=Count("items")),
            self.request.user,
        )
        params = self.request.query_params
        q = params.get("q", "").strip()
        if q:
            qs = qs.filter(Q(reference__icontains=q) | Q(customer__name__icontains=q)
                           | Q(customer__phone__icontains=q))
        if params.get("status"):
            qs = qs.filter(payment_status=params["status"], is_voided=False)
        if params.get("today") == "true":
            qs = qs.filter(created_at__date=timezone.localdate())
        if params.get("mine") == "true":
            qs = qs.filter(sold_by=self.request.user)
        return qs.order_by("-created_at")

    def create(self, request):
        """POST /api/sales/ - record a sale."""
        serializer = SaleCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        product_ids = [i["product"] for i in data["items"]]
        # Scoped lookup: another manager's product simply is not found, and
        # the error says exactly that rather than "you may not touch this",
        # which would confirm the ID belongs to someone.
        products = {
            p.pk: p
            for p in scoped(Product.objects.active(), request.user).filter(
                pk__in=product_ids
            )
        }

        cart = []
        for line in data["items"]:
            product = products.get(line["product"])
            if product is None:
                return Response(
                    {"detail": f"Product {line['product']} not found or inactive."},
                    status=400,
                )
            cart.append({
                "product": product,
                "quantity": line["quantity"],
                "unit_price": line.get("unit_price") or product.selling_price,
                "line_discount": line.get("line_discount") or 0,
            })

        customer = None
        if data.get("customer"):
            customer = scoped(Customer.objects.all(), request.user).filter(
                pk=data["customer"]
            ).first()
            if customer is None:
                return Response({"detail": "Customer not found."}, status=400)

        try:
            txn = create_sale(
                user=request.user, cart=cart, customer=customer,
                amount_paid=data["amount_paid"],
                discount_amount=data["discount_amount"],
                tax_amount=data["tax_amount"],
                payment_method=data["payment_method"],
                due_date=data.get("due_date"),
                notes=data.get("notes", ""),
            )
        except (SaleError, ValidationError) as exc:
            return _error(exc)

        # Receipts are attached after the sale, outside its atomic block: a bad
        # upload must never roll back completed stock movements.
        for f in request.FILES.getlist("receipt"):
            try:
                Receipt.objects.create(
                    transaction=txn, file=f, kind="SALE",
                    caption="Captured on mobile", uploaded_by=request.user,
                )
            except Exception:
                logger.exception("Receipt upload failed for %s", txn.reference)

        log_action(AuditAction.CREATE, instance=txn,
                   description=(f"Recorded sale {txn.reference} from the mobile app: "
                                f"total {txn.total_amount}, balance {txn.balance_due}."))
        return Response(
            TransactionSerializer(txn, context={"request": request}).data, status=201
        )

    @action(detail=True, methods=["post"], permission_classes=[IsAdmin])
    def void(self, request, pk=None):
        txn = self.get_object()
        reason = (request.data.get("reason") or "").strip()
        try:
            void_transaction(txn, user=request.user, reason=reason)
        except (SaleError, ValidationError) as exc:
            return _error(exc)
        log_action(AuditAction.VOID, instance=txn,
                   description=f"VOIDED {txn.reference} from the mobile app. Reason: {reason}")
        return Response(TransactionSerializer(txn, context={"request": request}).data)

    @action(detail=True, methods=["post"])
    def receipt(self, request, pk=None):
        txn = self.get_object()
        files = request.FILES.getlist("file")
        if not files:
            return Response({"detail": "No file supplied."}, status=400)
        created = [
            Receipt.objects.create(
                transaction=txn, file=f,
                kind=request.data.get("kind", "SALE"),
                caption=request.data.get("caption", ""),
                uploaded_by=request.user,
            )
            for f in files
        ]
        return Response({"attached": len(created)}, status=201)


# ---------------------------------------------------------------------------
# Credit
# ---------------------------------------------------------------------------
class DebtViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class = DebtSerializer
    permission_classes = [IsStaff]
    pagination_class = StandardPagination

    def get_queryset(self):
        qs = scoped(
            DebtRecord.objects.select_related("customer", "transaction", "owner"),
            self.request.user,
        )
        params = self.request.query_params
        q = params.get("q", "").strip()
        if q:
            qs = qs.filter(Q(reference__icontains=q) | Q(customer__name__icontains=q)
                           | Q(customer__phone__icontains=q))
        state = params.get("status", "")
        if state == "OVERDUE":
            qs = qs.overdue()
        elif state == "OPEN":
            qs = qs.open_debts()
        elif state:
            qs = qs.filter(status=state)
        return qs.order_by("status", "due_date")

    @action(detail=True, methods=["get"])
    def repayments(self, request, pk=None):
        qs = self.get_object().repayments.select_related("received_by").prefetch_related("proofs")
        return Response(
            RepaymentSerializer(qs, many=True, context={"request": request}).data
        )

    @action(detail=True, methods=["post"])
    def pay(self, request, pk=None):
        debt = self.get_object()
        serializer = RepaymentCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            repayment = record_repayment(
                debt=debt,
                amount=serializer.validated_data["amount"],
                user=request.user,
                method=serializer.validated_data.get("method", "CASH"),
                external_reference=serializer.validated_data.get("external_reference", ""),
                note=serializer.validated_data.get("note", ""),
                proof_files=request.FILES.getlist("proof"),
            )
        except (CreditError, ValidationError) as exc:
            return _error(exc)

        debt.refresh_from_db()
        log_action(AuditAction.PAYMENT, instance=repayment,
                   description=(f"Received {repayment.amount} from {debt.customer.name} "
                                f"against {debt.reference} via the mobile app."))
        return Response({
            "repayment": RepaymentSerializer(repayment, context={"request": request}).data,
            "debt": DebtSerializer(debt).data,
        }, status=201)

    @action(detail=True, methods=["post"], permission_classes=[IsAdmin])
    def write_off(self, request, pk=None):
        debt = self.get_object()
        reason = (request.data.get("reason") or "").strip()
        try:
            write_off_debt(debt=debt, user=request.user, reason=reason)
        except (CreditError, ValidationError) as exc:
            return _error(exc)
        log_action(AuditAction.OVERRIDE, instance=debt,
                   description=f"WROTE OFF {debt.reference} from the mobile app. Reason: {reason}")
        return Response(DebtSerializer(debt).data)


@api_view(["GET"])
@permission_classes([IsStaff])
def credit_overview(request):
    user = request.user
    receivables = receivables_summary(user=user)
    accounts = scoped(
        CreditAccount.objects.in_debt().select_related("customer"), user
    ).order_by("-outstanding_balance")[:10]
    due_soon = scoped(DebtRecord.objects.all(), user).due_within(7).select_related(
        "customer"
    )[:10]

    return Response({
        "outstanding": str(receivables["outstanding"]),
        "overdue_amount": str(receivables["overdue_amount"]),
        "open_count": receivables["debt_count"],
        "overdue_count": receivables["overdue_count"],
        "aging": {
            "buckets": {k: str(v) for k, v in receivables["aging"]["buckets"].items()},
            "counts": receivables["aging"]["counts"],
            "total": str(receivables["aging"]["total"]),
        },
        "top_borrowers": [
            {
                "customer_id": a.customer_id,
                "name": a.customer.name,
                "phone": a.customer.phone,
                "outstanding": str(a.outstanding_balance),
                "risk_level": a.risk_level,
                "risk_label": a.risk_label,
            }
            for a in accounts
        ],
        "due_soon": DebtSerializer(due_soon, many=True).data,
    })


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------
@api_view(["GET"])
@permission_classes([IsStaff])
def dashboard(request):
    """
    Everything the home screen shows, in one round trip.

    One request rather than six because this screen is opened on a phone, on
    a shop's mobile data, dozens of times a day. Every extra round trip is a
    visible stall on that connection.
    """
    today = timezone.localdate()
    month_start = today.replace(day=1)
    week_start = today - dt.timedelta(days=6)
    user = request.user

    today_stats = sales_summary(today, today, user=user)
    week_stats = sales_summary(week_start, today, user=user)
    month_stats = sales_summary(month_start, today, user=user)
    receivables = receivables_summary(user=user)
    today_collections = collections_summary(today, today, user=user)
    month_collections = collections_summary(month_start, today, user=user)

    products = scoped(Product.objects.alive(), user)
    customers = scoped(Customer.objects.all(), user)
    debts = scoped(DebtRecord.objects.all(), user)
    sales = scoped(Transaction.objects.active(), user)

    # 7-day trend, oldest first, so the chart reads left to right.
    trend = [
        {
            "date": row["date"].isoformat(),
            "label": row["label"],
            "weekday": row["date"].strftime("%a"),
            "revenue": str(row["revenue"]),
            "count": row["count"],
        }
        for row in daily_series(week_start, today, user=user)
    ]

    payload = {
        "today": {
            "revenue": str(today_stats["revenue"]),
            "collected": str(today_stats["collected"]),
            "outstanding": str(today_stats["outstanding"]),
            "count": today_stats["count"],
            # Cash taken today against OLD debts. A credit-heavy shop can have
            # a quiet sales day and a very good cash day; without this the
            # dashboard only ever shows half the till.
            "debt_collected": str(today_collections["collected"]),
        },
        "week": {
            "revenue": str(week_stats["revenue"]),
            "count": week_stats["count"],
        },
        "month": {
            "revenue": str(month_stats["revenue"]),
            "collected": str(month_stats["collected"]),
            "count": month_stats["count"],
            "average_sale": str(month_stats["average_sale"]),
            "debt_collected": str(month_collections["collected"]),
        },
        "receivables": {
            "outstanding": str(receivables["outstanding"]),
            "overdue_amount": str(receivables["overdue_amount"]),
            "open_count": receivables["debt_count"],
            "overdue_count": receivables["overdue_count"],
        },
        "inventory": {
            "product_count": products.filter(is_active=True).count(),
            "low_stock_count": products.needs_attention().count(),
            "out_of_stock_count": products.out_of_stock().count(),
        },
        "customers": {
            "total": customers.active().count(),
            "debtors": customers.with_debt().count(),
            "credit_approved": customers.credit_approved().count(),
        },
        "trend": trend,
        "top_products": [
            {
                "name": row["name"],
                "sku": row["sku"],
                "units": row["units"],
                "revenue": str(row["revenue"]),
            }
            for row in top_products(month_start, today, limit=5, user=user)
        ],
        "recent_sales": TransactionSerializer(
            sales.select_related("customer", "sold_by").order_by("-created_at")[:5],
            many=True, context={"request": request},
        ).data,
        "overdue_debts": DebtSerializer(
            debts.overdue().select_related("customer").order_by("due_date")[:5],
            many=True,
        ).data,
        "due_soon": DebtSerializer(
            debts.due_within(7).select_related("customer").order_by("due_date")[:5],
            many=True,
        ).data,
        "low_stock": ProductSerializer(
            products.needs_attention().order_by("stock_quantity")[:5],
            many=True, context={"request": request},
        ).data,
        "can_view_financials": user.can_view_financials,
        "is_admin": user.is_admin,
        "scope": "all" if sees_everything(user) else "own",
    }

    # Profit and stock valuation never reach a Manager's device.
    if user.can_view_financials:
        month_profit = profit_summary(month_start, today, user=user)
        valuation = inventory_valuation(user=user)
        payload["financials"] = {
            "month_cogs": str(month_profit["cogs"]),
            "month_gross_profit": str(month_profit["gross_profit"]),
            "month_margin_percent": str(month_profit["margin_percent"]),
            "stock_cost_value": str(valuation["cost_value"]),
            "stock_retail_value": str(valuation["retail_value"]),
            "potential_profit": str(valuation["potential_profit"]),
        }

    # Per-manager breakdown. Only an Admin has more than one row to compare,
    # so a Manager is not shown a one-line table of their own total again.
    if sees_everything(user):
        payload["by_manager"] = [
            {
                "name": row["name"],
                "count": row["count"],
                "revenue": str(row["revenue"]),
                "collected": str(row["collected"]),
                "outstanding": str(row["outstanding"]),
            }
            for row in sales_by_staff(month_start, today, user=user)
        ]

    return Response(payload)


@api_view(["GET"])
@permission_classes([IsAdmin])
def profit_report(request):
    """Admin only. Deliberately a separate endpoint from the dashboard."""
    def parse(raw, fallback):
        try:
            return dt.date.fromisoformat(raw)
        except (TypeError, ValueError):
            return fallback

    today = timezone.localdate()
    start = parse(request.query_params.get("date_from"), today - dt.timedelta(days=30))
    end = parse(request.query_params.get("date_to"), today)
    user = request.user

    data = profit_summary(start, end, user=user)
    return Response({
        "start": start, "end": end,
        "revenue": str(data["revenue"]),
        "cogs": str(data["cogs"]),
        "gross_profit": str(data["gross_profit"]),
        "margin_percent": str(data["margin_percent"]),
        "count": data["count"],
        "valuation": {k: str(v) for k, v in inventory_valuation(user=user).items()},
    })


@api_view(["GET"])
@permission_classes([])
def health(request):
    return Response({"status": "ok", "time": timezone.now().isoformat()})
