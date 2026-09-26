"""
What the business spends, and what it pays its people.

TWO MODELS, ONE IDEA
--------------------
An Expense is money going out: rent, fuel, a machine repair, a salary. An
Employee is somebody on the payroll - a loader, a guard, a driver - who does
not need an account in this system to be paid. Paying an employee IS an
expense, with the person attached, so "what did we spend this month" and
"what did we pay Kebede this year" come out of the same rows and can never
disagree.

Expenses are a ledger, owned like sales are (core.scoping): a manager sees
their own and their team's, the owner sees everything. Employees are shared,
like the product list - the payroll belongs to the business, and two managers
who both pay the same guard must both be able to find him.
"""
from decimal import Decimal

from django.conf import settings
from django.core.validators import MinValueValidator
from django.db import models
from django.db.models import Q, Sum
from django.urls import reverse
from django.utils import timezone

from core.models import AuthoredModel, OwnedModel, TimeStampedModel, note_tag_field
from core.utils import ZERO, receipt_upload_path, validate_receipt_file


class ExpenseMethod(models.TextChoices):
    """How the money left. No CREDIT: an unpaid bill is not yet an expense."""

    CASH = "CASH", "Cash"
    BANK = "BANK", "Bank transfer"
    MOBILE = "MOBILE", "Mobile money"
    CHEQUE = "CHEQUE", "Cheque"


class Employee(AuthoredModel):
    """Somebody on the payroll. No login needed."""

    name = models.CharField(max_length=160, db_index=True)
    # Managed pick-list plus a snapshot of the wording, like every other
    # Option reference (see core.options, rule 3).
    job = models.ForeignKey(
        "core.Option",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
        limit_choices_to={"group": "EMPLOYEE_JOB"},
    )
    job_name = models.CharField(max_length=120, blank=True)
    phone = models.CharField(max_length=30, blank=True)
    monthly_salary = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=ZERO,
        validators=[MinValueValidator(Decimal("0"))],
        help_text="What a normal month's salary is. Pre-fills the salary payment.",
    )
    hired_on = models.DateField(null=True, blank=True)
    is_active = models.BooleanField(
        default=True,
        db_index=True,
        help_text="Off for somebody who has left. Their pay history stays.",
    )
    notes = models.TextField(blank=True)
    note_tag = note_tag_field()

    class Meta:
        ordering = ["-is_active", "name"]
        constraints = [
            models.CheckConstraint(
                condition=Q(monthly_salary__gte=0),
                name="employee_salary_non_negative",
            ),
        ]

    def __str__(self):
        return self.name

    def get_absolute_url(self):
        return reverse("expenses:employee_detail", args=[self.pk])

    def paid_between(self, start, end) -> Decimal:
        """What this person was paid in a date range, voids excluded."""
        total = self.payments.filter(
            is_voided=False, spent_on__gte=start, spent_on__lte=end
        ).aggregate(t=Sum("amount"))["t"]
        return total or ZERO


class ExpenseQuerySet(models.QuerySet):
    def active(self):
        return self.filter(is_voided=False)

    def between(self, start, end):
        return self.active().filter(spent_on__gte=start, spent_on__lte=end)


class Expense(OwnedModel, TimeStampedModel):
    """One payment out of the business."""

    reference = models.CharField(
        max_length=40, unique=True, db_index=True, editable=False
    )
    spent_on = models.DateField(default=timezone.localdate, db_index=True)

    category = models.ForeignKey(
        "core.Option",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
        limit_choices_to={"group": "EXPENSE_CATEGORY"},
    )
    category_name = models.CharField(max_length=120, blank=True, db_index=True)

    amount = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        validators=[MinValueValidator(Decimal("0.01"))],
    )

    payment_method = models.CharField(
        max_length=10, choices=ExpenseMethod.choices, default=ExpenseMethod.CASH
    )
    payment_channel = models.ForeignKey(
        "core.Option",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
        help_text="Which bank or wallet it was paid from.",
    )
    payment_channel_name = models.CharField(max_length=120, blank=True)
    payment_reference = models.CharField(max_length=80, blank=True)

    payee = models.CharField(
        max_length=160, blank=True,
        help_text="Who was paid - the landlord, the garage, the fuel station.",
    )

    # -- Staff payments ------------------------------------------------------
    employee = models.ForeignKey(
        Employee,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="payments",
    )
    pay_type = models.ForeignKey(
        "core.Option",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
        limit_choices_to={"group": "EMPLOYEE_PAY_TYPE"},
    )
    pay_type_name = models.CharField(max_length=120, blank=True)
    pay_period = models.DateField(
        null=True,
        blank=True,
        help_text="The month a salary is for - its first day.",
    )

    receipt = models.FileField(
        upload_to=receipt_upload_path,
        validators=[validate_receipt_file],
        blank=True,
    )

    # -- Paid together ---------------------------------------------------------
    # One payment often covers several things - three workers paid out of one
    # envelope, fuel and a repair on one receipt. Each line is still its own
    # row, so "what did Kebede get" and "what went on fuel" keep working, and
    # the lines share the first one's reference here, so they can be shown -
    # and found - together.
    group_reference = models.CharField(
        max_length=40, blank=True, db_index=True,
        help_text="The reference of the first line of a payment with several lines.",
    )

    # -- What a batch cost -----------------------------------------------------
    # Labour, power, transport paid FOR one production batch. Still money out
    # of the business - it is listed and totalled with every other expense -
    # but it is also part of what that batch's units cost to make, so it goes
    # into the batch's cost per unit and from there into the product's cost
    # price. See production.services.record_production.
    production_run = models.ForeignKey(
        "production.ProductionRun",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="expenses",
    )

    recorded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="expenses_recorded",
    )
    notes = models.TextField(blank=True)
    note_tag = note_tag_field()

    is_voided = models.BooleanField(default=False, db_index=True)
    voided_at = models.DateTimeField(null=True, blank=True)
    voided_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="expenses_voided",
    )
    void_reason = models.TextField(blank=True)

    objects = ExpenseQuerySet.as_manager()

    class Meta:
        ordering = ["-spent_on", "-id"]
        indexes = [
            models.Index(fields=["owner", "-spent_on"], name="exp_owner_date_idx"),
            models.Index(fields=["employee", "-spent_on"], name="exp_employee_idx"),
        ]
        constraints = [
            models.CheckConstraint(
                condition=Q(amount__gt=0), name="expense_amount_positive"
            ),
        ]

    def __str__(self):
        return self.reference

    def get_absolute_url(self):
        return reverse("expenses:expense_edit", args=[self.pk])

    @property
    def reference_hint(self) -> str:
        """Used by receipt_upload_path to name the file after the expense."""
        return self.reference or "EXP"

    @property
    def is_staff_payment(self) -> bool:
        return self.employee_id is not None

    @property
    def is_batch_cost(self) -> bool:
        return self.production_run_id is not None

    def save(self, *args, **kwargs):
        if not self.reference:
            from core.utils import generate_reference

            self.reference = generate_reference("EXP", Expense)
        super().save(*args, **kwargs)
