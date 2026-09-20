"""
Expenses and employees.

    GET   /api/expenses/?month=2026-09          one month, newest first
    GET   /api/expenses/summary/?month=2026-09  totals, by category
    POST  /api/expenses/                        record (JSON or multipart)
    PATCH /api/expenses/<id>/                   correct
    POST  /api/expenses/<id>/void/              cancel, with a reason

    GET   /api/employees/                       the payroll
    POST  /api/employees/                       add somebody
    PATCH /api/employees/<id>/                  edit
    GET   /api/employees/<id>/payments/         what they were paid
"""
from django.core.exceptions import ValidationError
from django.db.models import Q
from django.utils import timezone
from rest_framework import mixins, status, viewsets
from rest_framework.decorators import action
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.response import Response

from accounts.models import AuditAction
from accounts.services import log_action
from core.models import resolve_option
from core.scoping import scoped
from expenses.models import Employee, Expense
from expenses.services import (
    ExpenseError,
    record_expense,
    summarize,
    update_expense,
    void_expense,
)

from .expense_serializers import (
    EmployeeSerializer,
    ExpenseSerializer,
    ExpenseWriteSerializer,
    month_bounds,
)
from .permissions import ActionPermission
from .serializers import ReasonSerializer
from .views import StandardPagination, _error


def _dated(qs, params, field="spent_on"):
    """Apply ?month=YYYY-MM, or ?from= / ?to=, to a queryset."""
    import datetime as dt

    bounds = month_bounds(params.get("month", ""))
    if bounds:
        return qs.filter(**{f"{field}__gte": bounds[0], f"{field}__lte": bounds[1]})
    for key, lookup in (("from", "gte"), ("to", "lte")):
        raw = params.get(key)
        if raw:
            try:
                qs = qs.filter(**{f"{field}__{lookup}": dt.date.fromisoformat(raw[:10])})
            except ValueError:
                pass
    return qs


class ExpenseViewSet(
    mixins.ListModelMixin, mixins.RetrieveModelMixin, viewsets.GenericViewSet
):
    serializer_class = ExpenseSerializer
    permission_classes = [ActionPermission]
    permission_map = {
        "GET": "expense.view",
        "POST": "expense.record",
        "PATCH": "expense.record",
        "PUT": "expense.record",
    }
    action_permissions = {"void": "expense.void", "summary": "expense.view"}
    pagination_class = StandardPagination
    # A receipt photo arrives as a file part, like a sale's.
    parser_classes = [JSONParser, MultiPartParser, FormParser]

    def get_queryset(self):
        qs = scoped(
            Expense.objects.select_related(
                "employee", "recorded_by", "voided_by", "owner", "note_tag",
            ),
            self.request.user,
        )
        params = self.request.query_params
        qs = _dated(qs, params)
        category = params.get("category")
        if category:
            qs = qs.filter(
                Q(category_id=category) if category.isdigit()
                else Q(category_name__iexact=category)
            )
        if params.get("employee"):
            qs = qs.filter(employee_id=params["employee"])
        if params.get("staff") == "true":
            qs = qs.filter(employee__isnull=False)
        q = (params.get("q") or "").strip()
        if q:
            qs = qs.filter(
                Q(reference__icontains=q) | Q(payee__icontains=q)
                | Q(category_name__icontains=q) | Q(notes__icontains=q)
            )
        state = params.get("state", "active")
        if state == "active":
            qs = qs.filter(is_voided=False)
        elif state == "voided":
            qs = qs.filter(is_voided=True)
        return qs.order_by("-spent_on", "-id")

    def _write_data(self, request):
        serializer = ExpenseWriteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        return dict(serializer.validated_data)

    def create(self, request):
        data = self._write_data(request)
        try:
            expense = record_expense(
                user=request.user, data=data, receipt=request.FILES.get("receipt")
            )
        except (ExpenseError, ValidationError) as exc:
            return _error(exc)
        log_action(
            AuditAction.CREATE,
            instance=expense,
            description=(
                f"Recorded expense {expense.reference}: {expense.amount} "
                f"for {expense.category_name}"
                + (f" ({expense.employee.name})" if expense.employee_id else "")
            ),
        )
        return Response(
            ExpenseSerializer(expense, context={"request": request}).data,
            status=status.HTTP_201_CREATED,
        )

    def partial_update(self, request, pk=None):
        expense = self.get_object()
        data = self._write_data(request)
        try:
            expense = update_expense(
                expense, user=request.user, data=data,
                receipt=request.FILES.get("receipt"),
            )
        except (ExpenseError, ValidationError) as exc:
            return _error(exc)
        log_action(
            AuditAction.UPDATE,
            instance=expense,
            description=f"Corrected expense {expense.reference}.",
        )
        return Response(ExpenseSerializer(expense, context={"request": request}).data)

    def update(self, request, pk=None):
        return self.partial_update(request, pk)

    @action(detail=True, methods=["post"])
    def void(self, request, pk=None):
        expense = self.get_object()
        serializer = ReasonSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        reason = serializer.validated_data["reason"]
        try:
            expense = void_expense(expense, user=request.user, reason=reason)
        except (ExpenseError, ValidationError) as exc:
            return _error(exc)
        log_action(
            AuditAction.VOID,
            instance=expense,
            description=f"Cancelled expense {expense.reference}: {reason}",
        )
        return Response(ExpenseSerializer(expense, context={"request": request}).data)

    @action(detail=False, methods=["get"])
    def summary(self, request):
        """
        Totals for the same filters as the list, voids excluded.

        Defaults to this month when no period is given, because "what have we
        spent this month?" is the question the screen opens on.
        """
        params = request.query_params
        base = scoped(Expense.objects.all(), request.user)
        if not (params.get("month") or params.get("from") or params.get("to")):
            today = timezone.localdate()
            base = base.filter(spent_on__gte=today.replace(day=1), spent_on__lte=today)
        else:
            base = _dated(base, params)
        result = summarize(base)
        return Response({
            "total": str(result["total"]),
            "count": result["count"],
            "staff_total": str(result["staff_total"]),
            "by_category": [
                {
                    "category": row["category"],
                    "total": str(row["total"]),
                    "count": row["count"],
                }
                for row in result["by_category"]
            ],
        })


class EmployeeViewSet(
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    mixins.CreateModelMixin,
    mixins.UpdateModelMixin,
    viewsets.GenericViewSet,
):
    serializer_class = EmployeeSerializer
    permission_classes = [ActionPermission]
    permission_map = {
        "GET": "employee.view",
        "POST": "employee.manage",
        "PATCH": "employee.manage",
        "PUT": "employee.manage",
    }
    action_permissions = {"payments": ("employee.view",)}

    def get_queryset(self):
        qs = Employee.objects.select_related("job", "note_tag")
        params = self.request.query_params
        if params.get("active") == "true":
            qs = qs.filter(is_active=True)
        q = (params.get("q") or "").strip()
        if q:
            qs = qs.filter(Q(name__icontains=q) | Q(phone__icontains=q)
                           | Q(job_name__icontains=q))
        return qs.order_by("-is_active", "name")

    def _job(self, serializer):
        """The job as picked from the list, or typed and added to it."""
        data = serializer.validated_data
        if "job" not in data and "job_name" not in data:
            return {}
        option = data.get("job")
        job, name = resolve_option(
            "EMPLOYEE_JOB",
            label=data.get("job_name", ""),
            option_id=option.pk if option is not None else None,
            user=self.request.user,
        )
        return {"job": job, "job_name": name}

    def perform_create(self, serializer):
        employee = serializer.save(
            created_by=self.request.user, **self._job(serializer)
        )
        log_action(
            AuditAction.CREATE, instance=employee,
            description=f"Added employee {employee.name}.",
        )

    def perform_update(self, serializer):
        employee = serializer.save(
            updated_by=self.request.user, **self._job(serializer)
        )
        log_action(
            AuditAction.UPDATE, instance=employee,
            description=f"Updated employee {employee.name}.",
        )

    @action(detail=True, methods=["get"])
    def payments(self, request, pk=None):
        """
        What this person was paid - only the payments the reader may see, so
        a manager sees what they paid and the owner sees all of it.
        """
        employee = self.get_object()
        qs = scoped(
            employee.payments.select_related("recorded_by", "note_tag"),
            request.user,
        ).order_by("-spent_on", "-id")
        qs = _dated(qs, request.query_params)
        return Response(
            ExpenseSerializer(qs[:200], many=True, context={"request": request}).data
        )
