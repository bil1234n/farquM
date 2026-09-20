"""
The web forms for expenses and the payroll.

The rules themselves - a bank transfer names its bank, a salary names its
person, nothing is dated in the future - live in expenses.services, which the
phone's API calls too. These forms only shape what the browser posts into the
same `data` dict the API builds, so the two front doors cannot drift apart.
"""
import datetime as dt
from decimal import Decimal

from django import forms
from django.utils import timezone

from accounts.forms import StyledFormMixin
from core.forms import NoteTagField, NoteTagFormMixin

from .models import Employee, ExpenseMethod


class MonthField(forms.CharField):
    """
    <input type="month"> - "2026-09" in, the 1st of that month out.

    A pay period is a month, not a day; storing it as the first of the month
    (see expenses.services.month_start) is what lets "who has had September's
    salary?" be a simple equality.
    """

    widget = forms.TextInput(attrs={"type": "month"})

    def to_python(self, value):
        value = (super().to_python(value) or "").strip()
        if not value:
            return None
        try:
            year, month = (int(part) for part in value.split("-")[:2])
            return dt.date(year, month, 1)
        except (TypeError, ValueError):
            raise forms.ValidationError("Choose a month.")

    def prepare_value(self, value):
        if isinstance(value, dt.date):
            return value.strftime("%Y-%m")
        return value


class ExpenseForm(StyledFormMixin, forms.Form):
    """Record or correct one expense."""

    spent_on = forms.DateField(
        label="Date",
        widget=forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"),
    )
    amount = forms.DecimalField(
        max_digits=14, decimal_places=2, min_value=Decimal("0.01"),
        label="Amount",
    )
    # A managed list that can grow from inside the form - see
    # static/js/option-select.js. The id when an entry was picked, the name
    # when a new one was typed; the service sorts out which.
    category = forms.IntegerField(required=False, widget=forms.HiddenInput)
    category_name = forms.CharField(
        required=False, max_length=120, widget=forms.HiddenInput
    )
    payee = forms.CharField(
        required=False, max_length=160, label="Paid to",
        help_text="The landlord, the garage, the fuel station.",
    )

    # -- Staff payments ------------------------------------------------------
    employee = forms.ModelChoiceField(
        queryset=Employee.objects.none(), required=False,
        empty_label="Not a staff payment",
        label="Employee",
    )
    pay_type = forms.IntegerField(required=False, widget=forms.HiddenInput)
    pay_type_name = forms.CharField(
        required=False, max_length=120, widget=forms.HiddenInput
    )
    pay_period = MonthField(
        required=False, label="For the month of",
        help_text="Which month's salary this is. Defaults to the month it was paid.",
    )

    # -- How it was paid -----------------------------------------------------
    payment_method = forms.ChoiceField(
        choices=ExpenseMethod.choices, initial=ExpenseMethod.CASH,
        label="Paid by",
    )
    payment_channel = forms.IntegerField(required=False, widget=forms.HiddenInput)
    payment_channel_name = forms.CharField(
        required=False, max_length=120, widget=forms.HiddenInput
    )
    payment_reference = forms.CharField(
        required=False, max_length=80, label="Transfer / cheque number",
    )
    receipt = forms.FileField(
        required=False, label="Receipt or invoice",
        help_text="Optional. A photo or PDF of the bill, up to 5 MB.",
    )
    notes = forms.CharField(
        required=False, widget=forms.Textarea(attrs={"rows": 2}), label="Notes"
    )
    note_tag = NoteTagField()

    def __init__(self, *args, **kwargs):
        self.instance = kwargs.pop("instance", None)
        super().__init__(*args, **kwargs)
        today = timezone.localdate()
        self.fields["spent_on"].initial = today
        self.fields["spent_on"].widget.attrs["max"] = today.isoformat()
        self.fields["amount"].widget.attrs.update({"step": "0.01", "min": "0.01"})

        current = getattr(self.instance, "employee_id", None)
        people = Employee.objects.filter(is_active=True)
        if current:
            # Somebody who has since left still shows on their old payments.
            people = Employee.objects.filter(pk=current) | people
        self.fields["employee"].queryset = people.select_related("job").order_by("name")
        self.fields["employee"].label_from_instance = (
            lambda e: f"{e.name}" + (f" - {e.job_name}" if e.job_name else "")
        )
        if self.instance is not None:
            self.fields["note_tag"] = NoteTagField(current=self.instance.note_tag_id)

    @classmethod
    def initial_for(cls, expense):
        """The form's starting values for an existing expense."""
        return {
            "spent_on": expense.spent_on,
            "amount": expense.amount,
            "category": expense.category_id,
            "category_name": expense.category_name,
            "payee": expense.payee,
            "employee": expense.employee_id,
            "pay_type": expense.pay_type_id,
            "pay_type_name": expense.pay_type_name,
            "pay_period": expense.pay_period,
            "payment_method": expense.payment_method,
            "payment_channel": expense.payment_channel_id,
            "payment_channel_name": expense.payment_channel_name,
            "payment_reference": expense.payment_reference,
            "notes": expense.notes,
            "note_tag": expense.note_tag_id,
        }

    def clean_receipt(self):
        upload = self.cleaned_data.get("receipt")
        if upload:
            from core.utils import validate_receipt_file

            validate_receipt_file(upload)
        return upload

    def service_data(self) -> dict:
        """What expenses.services expects - the same shape the API builds."""
        data = dict(self.cleaned_data)
        data.pop("receipt", None)
        return data


class VoidExpenseForm(StyledFormMixin, forms.Form):
    reason = forms.CharField(
        widget=forms.Textarea(attrs={"rows": 3}),
        label="Why is this expense being cancelled?",
        help_text="Kept on the record beside it. The expense stays visible, crossed out.",
    )


class EmployeeForm(NoteTagFormMixin, StyledFormMixin, forms.ModelForm):
    """Somebody on the payroll. No login needed - just who they are and pay."""

    job = forms.IntegerField(required=False, widget=forms.HiddenInput)
    job_name = forms.CharField(required=False, max_length=120, widget=forms.HiddenInput)

    class Meta:
        model = Employee
        fields = [
            "name", "phone", "monthly_salary", "hired_on", "is_active",
            "notes", "note_tag",
        ]
        widgets = {
            "hired_on": forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"),
            "notes": forms.Textarea(attrs={"rows": 2}),
        }
        labels = {"name": "Full name", "monthly_salary": "Monthly salary"}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance.pk:
            self.fields["job"].initial = self.instance.job_id
            self.fields["job_name"].initial = self.instance.job_name
        self.fields["monthly_salary"].widget.attrs.update({"step": "0.01", "min": "0"})

    def clean_name(self):
        name = " ".join((self.cleaned_data.get("name") or "").split())
        if not name:
            raise forms.ValidationError("Enter the employee's name.")
        return name

    def clean_monthly_salary(self):
        value = self.cleaned_data.get("monthly_salary")
        if value is not None and value < 0:
            raise forms.ValidationError("A salary cannot be negative.")
        return value or Decimal("0")
