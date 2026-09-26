"""Expenses and the people on the payroll, as the phone sees them."""
import datetime as dt
from decimal import Decimal

from django.db.models import Max, Sum
from django.utils import timezone
from rest_framework import serializers

from core.models import Option
from core.utils import ZERO
from expenses.models import Employee, Expense, ExpenseMethod

from .serializers import NOTE_TAG_FIELDS, NoteTagField, NoteTagMixin, OwnerNameMixin


class EmployeeSerializer(NoteTagMixin, serializers.ModelSerializer):
    """
    One person on the payroll, with what they were paid this month and this
    year - so the list answers "has Kebede had his salary?" without opening
    anybody.
    """

    job = serializers.PrimaryKeyRelatedField(
        queryset=Option.objects.filter(group="EMPLOYEE_JOB"),
        required=False,
        allow_null=True,
    )
    job_name = serializers.CharField(
        max_length=120, required=False, allow_blank=True
    )
    paid_this_month = serializers.SerializerMethodField()
    paid_this_year = serializers.SerializerMethodField()
    last_paid_on = serializers.SerializerMethodField()

    class Meta:
        model = Employee
        fields = [
            "id", "name", "job", "job_name", "phone", "monthly_salary",
            "hired_on", "is_active", "notes", *NOTE_TAG_FIELDS,
            "paid_this_month", "paid_this_year", "last_paid_on", "created_at",
        ]
        read_only_fields = ["created_at"]

    def _payments(self, obj):
        """
        Only the payments this reader may see. A manager looking at the shared
        payroll sees what THEY paid; the owner sees everything.
        """
        from core.scoping import scoped

        request = self.context.get("request")
        return scoped(
            obj.payments.filter(is_voided=False), getattr(request, "user", None)
        )

    def get_paid_this_month(self, obj) -> str:
        today = timezone.localdate()
        total = self._payments(obj).filter(
            spent_on__gte=today.replace(day=1), spent_on__lte=today
        ).aggregate(t=Sum("amount"))["t"]
        return str(total or ZERO)

    def get_paid_this_year(self, obj) -> str:
        today = timezone.localdate()
        total = self._payments(obj).filter(
            spent_on__gte=today.replace(month=1, day=1), spent_on__lte=today
        ).aggregate(t=Sum("amount"))["t"]
        return str(total or ZERO)

    def get_last_paid_on(self, obj):
        last = self._payments(obj).aggregate(d=Max("spent_on"))["d"]
        return last.isoformat() if last else None

    def validate_name(self, value):
        value = " ".join((value or "").split())
        if not value:
            raise serializers.ValidationError("Enter the employee's name.")
        return value

    def validate_monthly_salary(self, value):
        if value is not None and value < 0:
            raise serializers.ValidationError("A salary cannot be negative.")
        return value


class ExpenseSerializer(NoteTagMixin, OwnerNameMixin, serializers.ModelSerializer):
    """How an expense is read."""

    payment_method_display = serializers.CharField(
        source="get_payment_method_display", read_only=True
    )
    employee_name = serializers.CharField(
        source="employee.name", read_only=True, default=None
    )
    recorded_by_name = serializers.CharField(
        source="recorded_by.display_name", read_only=True, default=None
    )
    voided_by_name = serializers.CharField(
        source="voided_by.display_name", read_only=True, default=None
    )
    receipt_url = serializers.SerializerMethodField()
    owner_name = serializers.SerializerMethodField()
    #: The batch this was paid for, when it is one of a batch's costs.
    production_run_reference = serializers.CharField(
        source="production_run.reference", read_only=True, default=None
    )

    class Meta:
        model = Expense
        fields = [
            "id", "reference", "spent_on", "category", "category_name",
            "amount", "payment_method", "payment_method_display",
            "payment_channel", "payment_channel_name", "payment_reference",
            "payee", "employee", "employee_name", "pay_type", "pay_type_name",
            "pay_period", "notes", *NOTE_TAG_FIELDS, "receipt_url",
            "recorded_by_name", "created_at",
            "is_voided", "voided_at", "voided_by_name", "void_reason",
            "owner_name", "group_reference",
            "production_run", "production_run_reference",
        ]
        read_only_fields = fields

    def get_receipt_url(self, obj):
        if not obj.receipt:
            return None
        request = self.context.get("request")
        try:
            url = obj.receipt.url
        except Exception:
            return None
        return request.build_absolute_uri(url) if request else url


class ExpenseLineSerializer(serializers.Serializer):
    """
    One line of a payment with several: what differs from line to line. The
    date, how it was paid, the receipt and the note are the payment's.
    """

    amount = serializers.DecimalField(
        max_digits=14, decimal_places=2, min_value=Decimal("0.01")
    )
    category = serializers.IntegerField(required=False, allow_null=True)
    category_name = serializers.CharField(
        max_length=120, required=False, allow_blank=True, default=""
    )
    employee = serializers.IntegerField(required=False, allow_null=True)
    pay_type = serializers.IntegerField(required=False, allow_null=True)
    pay_type_name = serializers.CharField(
        max_length=120, required=False, allow_blank=True, default=""
    )
    payee = serializers.CharField(max_length=160, required=False, allow_blank=True)


class ExpenseWriteSerializer(serializers.Serializer):
    """
    What the phone sends to record or correct an expense. A list entry can
    arrive as the id somebody picked or the name somebody typed; the service
    resolves either, and adds a typed one to the list for next time.

    A payment of several lines - three workers paid at once, fuel and oil on
    one receipt - sends `lines`, and the fields here are the shared part.
    """

    amount = serializers.DecimalField(
        max_digits=14, decimal_places=2, min_value=Decimal("0.01"), required=False
    )
    lines = ExpenseLineSerializer(many=True, required=False)
    spent_on = serializers.DateField(required=False, allow_null=True)
    category = serializers.IntegerField(required=False, allow_null=True)
    category_name = serializers.CharField(
        max_length=120, required=False, allow_blank=True, default=""
    )
    payment_method = serializers.ChoiceField(
        choices=ExpenseMethod.choices, required=False, default=ExpenseMethod.CASH
    )
    payment_channel = serializers.IntegerField(required=False, allow_null=True)
    payment_channel_name = serializers.CharField(
        max_length=120, required=False, allow_blank=True, default=""
    )
    payment_reference = serializers.CharField(
        max_length=80, required=False, allow_blank=True, default=""
    )
    payee = serializers.CharField(
        max_length=160, required=False, allow_blank=True, default=""
    )
    employee = serializers.IntegerField(required=False, allow_null=True)
    pay_type = serializers.IntegerField(required=False, allow_null=True)
    pay_type_name = serializers.CharField(
        max_length=120, required=False, allow_blank=True, default=""
    )
    pay_period = serializers.DateField(required=False, allow_null=True)
    notes = serializers.CharField(required=False, allow_blank=True, default="")
    note_tag = NoteTagField()

    def validate_spent_on(self, value):
        if value and value > timezone.localdate():
            raise serializers.ValidationError("An expense cannot be dated in the future.")
        return value

    def validate(self, attrs):
        if not attrs.get("lines") and attrs.get("amount") is None:
            raise serializers.ValidationError({"amount": "Enter the amount."})
        return attrs


def month_bounds(text: str):
    """'2026-09' -> (2026-09-01, 2026-09-30). None for anything else."""
    try:
        year, month = (int(part) for part in (text or "").split("-")[:2])
        start = dt.date(year, month, 1)
    except (TypeError, ValueError):
        return None
    end = (start.replace(day=28) + dt.timedelta(days=4)).replace(day=1) - dt.timedelta(days=1)
    return start, end
