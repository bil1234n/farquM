"""
Serializers.

THE IMPORTANT RULE IN THIS FILE
-------------------------------
Cost prices and profit figures are removed from the response payload for a
Manager. Not hidden by the client - removed by the server, before the JSON
leaves the building. A client can be decompiled, patched, or replaced with
curl; the only place a permission means anything is on the server.
"""
from decimal import Decimal

from django.contrib.auth import authenticate
from rest_framework import serializers

from accounts.models import DataScope, RoleDefinition, User
from core.models import SystemSetting
from core.permissions import WILDCARD, clean_codes
from credit.models import CreditAccount, DebtRecord, Repayment, RepaymentProof
from inventory.models import Category, Product, StockMovement, Supplier
from sales.models import Customer, Receipt, Transaction, TransactionItem

from .models import DeviceToken, NotificationLog


class FinancialFieldsMixin:
    """
    Strips cost/profit keys unless the requesting user may see them.

    `financial_fields` are cost figures, gated on `product.view_cost`.
    `profit_fields` are margin figures, gated on `report.profit` - a separate,
    stricter permission, because a manager who buys the stock legitimately
    needs to know what it cost without also being shown the mark-up.
    """

    financial_fields: tuple = ()
    profit_fields: tuple = ()

    def to_representation(self, instance):
        data = super().to_representation(instance)
        request = self.context.get("request")
        user = getattr(request, "user", None)

        if user is None or not getattr(user, "can_view_costs", False):
            for field in self.financial_fields:
                data.pop(field, None)

        if user is None or not getattr(user, "can_view_profit", False):
            for field in self.profit_fields:
                data.pop(field, None)
        return data


class OwnerNameMixin:
    """
    Adds a read-only `owner_name` telling you whose record this is.

    Populated only for someone who can see more than their own data - i.e. an
    Admin. For a Manager every row is theirs by definition, so the label would
    be noise; worse, printing it on every card advertises that "other people's
    records" is a category worth poking at. Managers get null.

    Declare `owner_name = serializers.SerializerMethodField()` on the
    serializer and mix this in.
    """

    def get_owner_name(self, obj):
        from core.scoping import sees_everything

        request = self.context.get("request")
        if not sees_everything(getattr(request, "user", None)):
            return None
        owner = getattr(obj, "owner", None)
        return owner.display_name if owner else "Unassigned"


# ---------------------------------------------------------------------------
# Auth & users
# ---------------------------------------------------------------------------
class LoginSerializer(serializers.Serializer):
    username = serializers.CharField()
    password = serializers.CharField(write_only=True, style={"input_type": "password"})

    def validate(self, attrs):
        user = authenticate(
            request=self.context.get("request"),
            username=attrs["username"],
            password=attrs["password"],
        )
        if user is None:
            raise serializers.ValidationError("Incorrect username or password.")
        if not user.is_active:
            raise serializers.ValidationError(
                "This account has been deactivated. Contact an administrator."
            )
        attrs["user"] = user
        return attrs


class UserSerializer(serializers.ModelSerializer):
    """The signed-in user's own profile. Role and status are read-only here."""

    display_name = serializers.CharField(read_only=True)
    role_display = serializers.CharField(source="get_role_display", read_only=True)
    permissions = serializers.SerializerMethodField()
    avatar_url = serializers.SerializerMethodField()
    initials = serializers.CharField(read_only=True)

    class Meta:
        model = User
        fields = [
            "id", "username", "first_name", "last_name", "display_name",
            "email", "phone", "role", "role_display", "is_active", "permissions",
            "avatar", "avatar_url", "initials", "employee_id", "date_joined",
            "last_login",
        ]
        # role and is_active are NOT editable through this serializer. A user
        # promoting themselves to Admin by PATCHing their own profile would
        # defeat the whole data-isolation model, so the field is read-only
        # here and only writable through the admin-only UserAdminSerializer.
        read_only_fields = [
            "id", "username", "role", "is_active", "employee_id",
            "date_joined", "last_login",
        ]
        extra_kwargs = {"avatar": {"write_only": True, "required": False}}

    def get_permissions(self, obj):
        """
        The signed-in user's effective access.

        `codes` is the authoritative list - every screen in the app should
        gate on it. The named booleans above it are the four flags older
        builds of the app read, kept so an un-updated phone keeps working
        rather than losing every button at once after a server deploy.
        """
        return {
            "view_financials": obj.can_view_costs,
            "manage_users": obj.can_manage_users,
            "delete_records": obj.can_delete_records,
            "change_settings": obj.can_change_settings,
            "view_costs": obj.can_view_costs,
            "view_profit": obj.can_view_profit,
            "codes": sorted(obj.effective_permissions),
            "data_scope": obj.data_scope,
            "data_scope_label": obj.scope_label,
            "manager": obj.manager.display_name if obj.manager_id else None,
        }

    def get_avatar_url(self, obj):
        url = obj.avatar_url
        if not url:
            return None
        request = self.context.get("request")
        # Cloudinary already returns an absolute URL; build_absolute_uri would
        # leave it untouched, but local-storage paths are relative and the
        # phone has no idea what host to prefix them with.
        if request and url.startswith("/"):
            return request.build_absolute_uri(url)
        return url


class UserAdminSerializer(UserSerializer):
    """
    Admin-facing view of ANOTHER user. Adds the fields an administrator is
    allowed to change, which the self-service serializer deliberately locks.
    """

    password = serializers.CharField(
        write_only=True, required=False, allow_blank=True, min_length=8
    )
    sales_count = serializers.IntegerField(read_only=True, required=False)
    outstanding = serializers.SerializerMethodField()

    manager_name = serializers.CharField(
        source="manager.display_name", default=None, read_only=True
    )
    data_scope = serializers.CharField(read_only=True)
    scope_label = serializers.CharField(read_only=True)
    is_customised = serializers.SerializerMethodField()

    class Meta(UserSerializer.Meta):
        fields = UserSerializer.Meta.fields + [
            "password", "notes", "sales_count", "outstanding", "last_activity",
            "manager", "manager_name", "data_scope", "scope_label",
            "data_scope_override", "is_customised",
        ]
        read_only_fields = ["id", "date_joined", "last_login", "last_activity"]
        extra_kwargs = {"avatar": {"write_only": True, "required": False}}

    def get_is_customised(self, obj):
        return bool(
            obj.extra_permissions or obj.denied_permissions or obj.data_scope_override
        )

    def get_outstanding(self, obj):
        """How much credit this manager has out on the street."""
        from decimal import Decimal

        from django.db.models import Sum

        from credit.models import DebtRecord

        total = (
            DebtRecord.objects.filter(owner=obj)
            .open_debts()
            .aggregate(t=Sum("balance"))["t"]
        )
        return str(total or Decimal("0.00"))

    def validate_role(self, value):
        """
        Never let the last active administrator be demoted.

        Without this the system can be locked into a state where nobody can
        manage users, approve credit, or see the full books - recoverable only
        from a Django shell on the server.
        """
        if not RoleDefinition.objects.filter(code=value, is_active=True).exists():
            raise serializers.ValidationError("Unknown role.")
        instance = self.instance
        if (
            instance
            and instance.role == "ADMIN"
            and value != "ADMIN"
            and not User.objects.admins()
            .filter(is_active=True)
            .exclude(pk=instance.pk)
            .exists()
        ):
            raise serializers.ValidationError(
                "This is the only active administrator. Promote someone else first."
            )
        return value

    def validate_is_active(self, value):
        instance = self.instance
        request = self.context.get("request")
        if not value and instance:
            if request and instance.pk == getattr(request.user, "pk", None):
                raise serializers.ValidationError(
                    "You cannot deactivate your own account."
                )
            if (
                instance.role == "ADMIN"
                and not User.objects.admins()
                .filter(is_active=True)
                .exclude(pk=instance.pk)
                .exists()
            ):
                raise serializers.ValidationError(
                    "Cannot deactivate the only remaining administrator."
                )
        return value

    def create(self, validated_data):
        password = validated_data.pop("password", None)
        if not password:
            raise serializers.ValidationError(
                {"password": "A password is required when creating a user."}
            )
        user = User(**validated_data)
        user.set_password(password)
        user.save()
        return user

    def update(self, instance, validated_data):
        password = validated_data.pop("password", None)
        user = super().update(instance, validated_data)
        if password:
            user.set_password(password)
            # Forcing a change means an admin-set password is a one-time key,
            # not a credential the admin permanently knows.
            user.must_change_password = True
            user.save(update_fields=["password", "must_change_password"])
        return user


# ---------------------------------------------------------------------------
# Inventory
# ---------------------------------------------------------------------------
class CategorySerializer(serializers.ModelSerializer):
    product_count = serializers.IntegerField(read_only=True)

    class Meta:
        model = Category
        fields = ["id", "name", "description", "is_active", "product_count"]


class SupplierSerializer(serializers.ModelSerializer):
    class Meta:
        model = Supplier
        fields = ["id", "name", "contact_person", "phone", "email", "address", "is_active"]


class ProductSerializer(OwnerNameMixin, FinancialFieldsMixin, serializers.ModelSerializer):
    financial_fields = ("cost_price", "stock_value")
    profit_fields = ("margin_percent", "profit_per_unit")

    category_name = serializers.CharField(source="category.name", default=None, read_only=True)
    supplier_name = serializers.CharField(source="supplier.name", default=None, read_only=True)
    unit_display = serializers.CharField(source="get_unit_display", read_only=True)
    stock_status = serializers.CharField(read_only=True)
    stock_status_label = serializers.CharField(read_only=True)
    margin_percent = serializers.DecimalField(max_digits=6, decimal_places=2, read_only=True)
    stock_value = serializers.DecimalField(max_digits=14, decimal_places=2, read_only=True)
    profit_per_unit = serializers.DecimalField(max_digits=12, decimal_places=2, read_only=True)
    image_url = serializers.SerializerMethodField()
    owner_name = serializers.SerializerMethodField()

    # Writable, so the phone can attach a photo the same way the web form
    # does. `image_url` stays the read side - a client should never have to
    # know how the storage backend builds a path, and on Cloudinary it is not
    # a path at all.
    #
    # required=False AND allow_null: a PATCH that does not mention the image
    # must leave it alone, and an explicit null must clear it. Without
    # allow_null the only way to remove a photo would be to delete the
    # product.
    image = serializers.ImageField(
        required=False, allow_null=True, write_only=True
    )
    #: So a list view can show a placeholder without fetching every photo.
    has_image = serializers.SerializerMethodField()

    class Meta:
        model = Product
        fields = [
            "id", "name", "sku", "barcode", "description",
            "category", "category_name", "supplier", "supplier_name",
            "unit", "unit_display",
            "cost_price", "selling_price", "profit_per_unit", "margin_percent",
            "stock_quantity", "low_stock_threshold", "stock_value",
            "stock_status", "stock_status_label", "is_active",
            "image", "image_url", "has_image",
            "owner_name",
        ]
        read_only_fields = ["id", "stock_quantity", "has_image"]

    def get_has_image(self, obj) -> bool:
        return bool(obj.image)

    def get_image_url(self, obj):
        if not obj.image:
            return None
        request = self.context.get("request")
        url = obj.image.url
        return request.build_absolute_uri(url) if request else url


class StockMovementSerializer(serializers.ModelSerializer):
    product_name = serializers.CharField(source="product.name", read_only=True)
    product_sku = serializers.CharField(source="product.sku", read_only=True)
    movement_type_display = serializers.CharField(source="get_movement_type_display", read_only=True)
    performed_by_name = serializers.CharField(
        source="performed_by.display_name", default=None, read_only=True
    )

    class Meta:
        model = StockMovement
        fields = [
            "id", "product", "product_name", "product_sku",
            "movement_type", "movement_type_display",
            "quantity_delta", "quantity_before", "quantity_after",
            "reference", "reason", "performed_by_name", "created_at",
        ]


class RestockSerializer(serializers.Serializer):
    quantity = serializers.IntegerField(min_value=1)
    unit_cost = serializers.DecimalField(
        max_digits=12, decimal_places=2, required=False, allow_null=True
    )
    reference = serializers.CharField(max_length=60, required=False, allow_blank=True)
    reason = serializers.CharField(max_length=255, required=False, allow_blank=True)


class StockAdjustSerializer(serializers.Serializer):
    """
    Damage out, or a customer return in.

    `kind` is restricted to those two on purpose. RESTOCK, SALE and
    VOID_REVERSAL are written by their own services with their own rules, and
    letting a client name any movement type would be a way to fabricate a
    delivery or a sale that no money ever passed through.
    """

    KINDS = (
        ("DAMAGE", "Damage / write-off"),
        ("RETURN_IN", "Customer return (in)"),
    )

    kind = serializers.ChoiceField(choices=KINDS)
    quantity = serializers.IntegerField(min_value=1)
    reason = serializers.CharField(max_length=255, required=False, allow_blank=True)
    reference = serializers.CharField(max_length=60, required=False, allow_blank=True)


class StockRecountSerializer(serializers.Serializer):
    """A stock-take: the number you counted on the shelf, not a difference."""

    counted_quantity = serializers.IntegerField(min_value=0)
    reason = serializers.CharField(max_length=255, required=False, allow_blank=True)


class RescheduleSerializer(serializers.Serializer):
    """Move a debt's due date."""

    due_date = serializers.DateField()
    reason = serializers.CharField(max_length=255, required=False, allow_blank=True)

    def validate_due_date(self, value):
        from django.utils import timezone

        if value < timezone.localdate():
            raise serializers.ValidationError("The due date cannot be in the past.")
        return value


class CreditLimitSerializer(serializers.Serializer):
    """How much this customer may owe at once."""

    credit_limit = serializers.DecimalField(
        max_digits=14, decimal_places=2, min_value=0
    )
    reason = serializers.CharField(max_length=255, required=False, allow_blank=True)


# ---------------------------------------------------------------------------
# Customers & credit summary
# ---------------------------------------------------------------------------
class CreditAccountSerializer(serializers.ModelSerializer):
    risk_level = serializers.CharField(read_only=True)
    risk_label = serializers.CharField(read_only=True)
    available_credit = serializers.DecimalField(max_digits=14, decimal_places=2, read_only=True)
    utilisation_percent = serializers.DecimalField(max_digits=8, decimal_places=2, read_only=True)
    is_over_limit = serializers.BooleanField(read_only=True)

    class Meta:
        model = CreditAccount
        fields = [
            "id", "credit_limit", "default_terms_days",
            "total_credit_extended", "total_repaid", "outstanding_balance",
            "available_credit", "utilisation_percent", "is_over_limit",
            "is_blocked", "block_reason", "risk_level", "risk_label",
            "last_purchase_date", "last_payment_date",
        ]
        read_only_fields = [
            "total_credit_extended", "total_repaid", "outstanding_balance",
            "last_purchase_date", "last_payment_date",
        ]


class CustomerSerializer(OwnerNameMixin, serializers.ModelSerializer):
    outstanding_balance = serializers.DecimalField(max_digits=14, decimal_places=2, read_only=True)
    credit_limit = serializers.DecimalField(max_digits=14, decimal_places=2, read_only=True)
    available_credit = serializers.DecimalField(max_digits=14, decimal_places=2, read_only=True)
    customer_type_display = serializers.CharField(source="get_customer_type_display", read_only=True)
    credit_account = CreditAccountSerializer(read_only=True)
    owner_name = serializers.SerializerMethodField()

    class Meta:
        model = Customer
        fields = [
            "id", "name", "phone", "alternate_phone", "email", "address",
            "customer_type", "customer_type_display",
            "is_credit_approved", "is_active", "notes",
            "outstanding_balance", "credit_limit", "available_credit", "credit_account",
            "owner_name",
        ]

    def validate_is_credit_approved(self, value):
        """Granting credit is a financial decision - Admin only."""
        request = self.context.get("request")
        user = getattr(request, "user", None)
        if value and user is not None and not user.is_admin:
            raise serializers.ValidationError(
                "Only an administrator may approve a customer for credit."
            )
        return value


# ---------------------------------------------------------------------------
# Sales
# ---------------------------------------------------------------------------
class TransactionItemSerializer(FinancialFieldsMixin, serializers.ModelSerializer):
    financial_fields = ("unit_cost", "line_cost")
    profit_fields = ("line_profit",)

    line_cost = serializers.DecimalField(max_digits=14, decimal_places=2, read_only=True)
    line_profit = serializers.DecimalField(max_digits=14, decimal_places=2, read_only=True)

    class Meta:
        model = TransactionItem
        fields = [
            "id", "product", "product_name", "product_sku", "quantity",
            "unit_price", "unit_cost", "line_discount", "line_total",
            "line_cost", "line_profit",
        ]


class ReceiptSerializer(serializers.ModelSerializer):
    file_url = serializers.SerializerMethodField()
    kind_display = serializers.CharField(source="get_kind_display", read_only=True)
    uploaded_by_name = serializers.CharField(
        source="uploaded_by.display_name", default=None, read_only=True
    )
    is_image = serializers.BooleanField(read_only=True)

    class Meta:
        model = Receipt
        fields = [
            "id", "file_url", "kind", "kind_display", "caption",
            "is_image", "uploaded_by_name", "created_at",
        ]

    def get_file_url(self, obj):
        """
        Absolute, because the phone is not on the same origin as the server
        and a relative /media/... path would resolve against the app itself.
        """
        request = self.context.get("request")
        try:
            url = obj.file.url
        except Exception:
            # A row whose blob has gone - a real risk when switching storage
            # backends. A missing photo must not break the whole sale.
            return None
        return request.build_absolute_uri(url) if request else url


class TransactionSerializer(OwnerNameMixin, FinancialFieldsMixin, serializers.ModelSerializer):
    financial_fields = ("total_cost",)
    profit_fields = ("gross_profit", "profit_margin")

    items = TransactionItemSerializer(many=True, read_only=True)
    receipts = ReceiptSerializer(many=True, read_only=True)
    customer_display = serializers.CharField(read_only=True)
    payment_status_display = serializers.CharField(source="get_payment_status_display", read_only=True)
    payment_method_display = serializers.CharField(source="get_payment_method_display", read_only=True)
    sold_by_name = serializers.CharField(source="sold_by.display_name", default=None, read_only=True)
    total_cost = serializers.DecimalField(max_digits=14, decimal_places=2, read_only=True)
    gross_profit = serializers.DecimalField(max_digits=14, decimal_places=2, read_only=True)
    profit_margin = serializers.DecimalField(max_digits=8, decimal_places=2, read_only=True)
    is_overdue = serializers.BooleanField(read_only=True)
    item_count = serializers.IntegerField(read_only=True)
    owner_name = serializers.SerializerMethodField()

    class Meta:
        model = Transaction
        fields = [
            "id", "reference", "customer", "customer_display",
            "subtotal", "discount_amount", "tax_amount", "total_amount",
            "amount_paid", "balance_due",
            "payment_status", "payment_status_display",
            "payment_method", "payment_method_display",
            "due_date", "notes", "sold_by_name", "created_at",
            "is_voided", "void_reason", "is_overdue", "item_count",
            "total_cost", "gross_profit", "profit_margin",
            "items", "receipts", "owner_name",
        ]


class SaleItemInputSerializer(serializers.Serializer):
    product = serializers.IntegerField()
    quantity = serializers.IntegerField(min_value=1)
    unit_price = serializers.DecimalField(
        max_digits=12, decimal_places=2, required=False, allow_null=True
    )
    line_discount = serializers.DecimalField(
        max_digits=12, decimal_places=2, required=False, default=Decimal("0.00")
    )


class SaleCreateSerializer(serializers.Serializer):
    """
    Input for POST /api/sales/. Validation of stock and credit limits is NOT
    duplicated here - it happens inside sales.services.create_sale(), which the
    web UI uses too. One implementation, one set of rules.
    """

    items = SaleItemInputSerializer(many=True)
    customer = serializers.IntegerField(required=False, allow_null=True)
    amount_paid = serializers.DecimalField(
        max_digits=14, decimal_places=2, default=Decimal("0.00")
    )
    discount_amount = serializers.DecimalField(
        max_digits=14, decimal_places=2, default=Decimal("0.00")
    )
    tax_amount = serializers.DecimalField(
        max_digits=14, decimal_places=2, default=Decimal("0.00")
    )
    payment_method = serializers.CharField(default="CASH")
    due_date = serializers.DateField(required=False, allow_null=True)
    notes = serializers.CharField(required=False, allow_blank=True, default="")

    def validate_items(self, value):
        if not value:
            raise serializers.ValidationError("A sale needs at least one item.")
        return value


# ---------------------------------------------------------------------------
# Credit
# ---------------------------------------------------------------------------
class RepaymentProofSerializer(serializers.ModelSerializer):
    file_url = serializers.SerializerMethodField()

    class Meta:
        model = RepaymentProof
        fields = ["id", "file_url", "caption", "created_at"]

    def get_file_url(self, obj):
        request = self.context.get("request")
        return request.build_absolute_uri(obj.file.url) if request else obj.file.url


class RepaymentSerializer(serializers.ModelSerializer):
    method_display = serializers.CharField(source="get_method_display", read_only=True)
    received_by_name = serializers.CharField(
        source="received_by.display_name", default=None, read_only=True
    )
    proofs = RepaymentProofSerializer(many=True, read_only=True)

    class Meta:
        model = Repayment
        fields = [
            "id", "reference", "debt", "amount", "method", "method_display",
            "paid_at", "balance_before", "balance_after", "external_reference",
            "note", "received_by_name", "is_reversed", "reversal_reason", "proofs",
        ]


class DebtSerializer(OwnerNameMixin, serializers.ModelSerializer):
    customer_name = serializers.CharField(source="customer.name", read_only=True)
    customer_phone = serializers.CharField(source="customer.phone", read_only=True)
    transaction_reference = serializers.CharField(
        source="transaction.reference", default=None, read_only=True
    )
    status_display = serializers.CharField(source="get_status_display", read_only=True)
    display_status = serializers.CharField(read_only=True)
    is_overdue = serializers.BooleanField(read_only=True)
    days_overdue = serializers.IntegerField(read_only=True)
    repayment_percent = serializers.DecimalField(max_digits=6, decimal_places=2, read_only=True)
    aging_bucket = serializers.CharField(read_only=True)
    owner_name = serializers.SerializerMethodField()

    class Meta:
        model = DebtRecord
        fields = [
            "id", "reference", "customer", "customer_name", "customer_phone",
            "transaction", "transaction_reference",
            "principal", "amount_repaid", "balance",
            "status", "status_display", "display_status",
            "issued_date", "due_date", "settled_date",
            "is_overdue", "days_overdue", "repayment_percent", "aging_bucket",
            "notes", "created_at", "owner_name",
        ]


class RepaymentCreateSerializer(serializers.Serializer):
    amount = serializers.DecimalField(max_digits=14, decimal_places=2, min_value=Decimal("0.01"))
    method = serializers.CharField(default="CASH")
    external_reference = serializers.CharField(required=False, allow_blank=True, default="")
    note = serializers.CharField(required=False, allow_blank=True, default="")
    proof = serializers.ListField(
        child=serializers.FileField(), required=False, allow_empty=True
    )


# ---------------------------------------------------------------------------
# Devices & notifications
# ---------------------------------------------------------------------------
class DeviceTokenSerializer(serializers.ModelSerializer):
    class Meta:
        model = DeviceToken
        fields = ["id", "token", "platform", "device_name", "app_version", "last_seen"]
        read_only_fields = ["id", "last_seen"]


class NotificationSerializer(serializers.ModelSerializer):
    is_read = serializers.BooleanField(read_only=True)

    class Meta:
        model = NotificationLog
        fields = ["id", "title", "body", "channel", "data", "created_at", "read_at", "is_read"]


# ---------------------------------------------------------------------------
# Access control
# ---------------------------------------------------------------------------
class PermissionCodeListField(serializers.ListField):
    """
    A list of permission codes, filtered against the catalogue on the way in.

    Unknown codes are dropped rather than rejected. A phone running an older
    build sends the codes it knows about; failing the whole save because one
    of them has since been renamed would make the app unusable after a server
    deploy, and silently ignoring a code that does not exist grants nothing.
    """

    child = serializers.CharField()

    def to_internal_value(self, data):
        codes = super().to_internal_value(data)
        if WILDCARD in codes:
            return [WILDCARD]
        return clean_codes(codes)


class RoleDefinitionSerializer(serializers.ModelSerializer):
    permissions = PermissionCodeListField(required=False)
    permission_count = serializers.IntegerField(read_only=True)
    user_count = serializers.IntegerField(read_only=True)
    is_full_access = serializers.BooleanField(read_only=True)
    can_be_deleted = serializers.BooleanField(read_only=True)
    data_scope_display = serializers.CharField(
        source="get_data_scope_display", read_only=True
    )

    class Meta:
        model = RoleDefinition
        fields = [
            "id", "code", "name", "description", "permissions", "data_scope",
            "data_scope_display", "rank", "is_system", "is_active",
            "permission_count", "user_count", "is_full_access", "can_be_deleted",
        ]
        read_only_fields = ["id", "is_system"]

    def validate_code(self, value):
        code = (value or "").strip().upper().replace(" ", "_")
        if self.instance:
            # The code is written into every user row holding this role;
            # changing it would orphan all of them at once.
            return self.instance.code
        if not code:
            raise serializers.ValidationError("A code is required.")
        if not code.replace("_", "").isalnum():
            raise serializers.ValidationError(
                "Use letters, digits and underscores only."
            )
        if RoleDefinition.objects.filter(code=code).exists():
            raise serializers.ValidationError("A role with that code already exists.")
        return code

    def validate(self, attrs):
        instance = self.instance
        if instance and instance.code == "ADMIN":
            if attrs.get("data_scope", instance.data_scope) != DataScope.ALL:
                raise serializers.ValidationError(
                    {"data_scope": "The Administrator role must see every record."}
                )
        return attrs


class UserAccessSerializer(serializers.Serializer):
    """
    What the phone sends back after somebody finishes ticking boxes.

    Only the ticked codes are sent - never a pre-computed list of grants and
    denials. Working out the difference from the role is the server's job, in
    core.access, so the two clients cannot disagree about what a tick means.
    """

    role = serializers.CharField(required=False, allow_blank=True)
    manager = serializers.IntegerField(required=False, allow_null=True)
    data_scope_override = serializers.ChoiceField(
        choices=[("", "Use the role's setting")] + list(DataScope.choices),
        required=False,
        allow_blank=True,
        default="",
    )
    permissions = PermissionCodeListField()

    def validate_role(self, value):
        if value and not RoleDefinition.objects.filter(
            code=value, is_active=True
        ).exists():
            raise serializers.ValidationError("That role no longer exists.")
        return value


class SystemSettingSerializer(serializers.ModelSerializer):
    business_name_effective = serializers.CharField(source="name", read_only=True)
    currency_effective = serializers.CharField(source="currency", read_only=True)

    class Meta:
        model = SystemSetting
        fields = [
            "business_name", "business_phone", "business_email",
            "business_address", "currency_symbol", "default_credit_due_days",
            "low_stock_threshold", "allow_self_registration",
            "require_credit_approval", "updated_at",
            "business_name_effective", "currency_effective",
        ]
        read_only_fields = ["updated_at"]
