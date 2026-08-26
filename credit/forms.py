from decimal import Decimal

from django import forms
from django.utils import timezone

from accounts.forms import StyledFormMixin

from .models import CreditAccount, DebtRecord, Repayment


class MultiFileInput(forms.ClearableFileInput):
    allow_multiple_selected = True


class RepaymentForm(StyledFormMixin, forms.Form):
    """
    Record one installment against a debt.

    The debt is passed in so the form can validate the amount against the
    live outstanding balance rather than trusting the browser.
    """

    amount = forms.DecimalField(
        max_digits=14, decimal_places=2, min_value=Decimal("0.01"),
        label="Amount received",
    )
    method = forms.ChoiceField(
        choices=Repayment.Method.choices, initial=Repayment.Method.CASH,
        label="Payment method",
    )
    paid_at = forms.DateTimeField(
        required=False,
        widget=forms.DateTimeInput(attrs={"type": "datetime-local"}),
        label="Received at",
        help_text="Leave blank to use the current time.",
    )
    external_reference = forms.CharField(
        max_length=100, required=False, label="Bank / mobile-money reference",
        help_text="Slip number, transaction ID or cheque number.",
    )
    note = forms.CharField(
        required=False, widget=forms.Textarea(attrs={"rows": 2}), label="Note"
    )
    proof = forms.FileField(
        required=False, widget=MultiFileInput(attrs={"multiple": True}),
        label="Receipt / proof of payment",
        help_text="Image or PDF. You can attach more than one file.",
    )

    def __init__(self, *args, **kwargs):
        self.debt = kwargs.pop("debt", None)
        self.user = kwargs.pop("user", None)
        super().__init__(*args, **kwargs)
        if self.debt is not None:
            self.fields["amount"].initial = self.debt.balance
            self.fields["amount"].help_text = (
                f"Outstanding balance: {self.debt.balance}. "
                f"Enter the full amount to settle this debt."
            )
            self.fields["amount"].widget.attrs["max"] = str(self.debt.balance)
            self.fields["amount"].widget.attrs["step"] = "0.01"

    def clean_paid_at(self):
        paid_at = self.cleaned_data.get("paid_at")
        if paid_at:
            if timezone.is_naive(paid_at):
                paid_at = timezone.make_aware(paid_at)
            if paid_at > timezone.now():
                raise forms.ValidationError("A payment cannot be dated in the future.")
        return paid_at

    def clean_amount(self):
        amount = self.cleaned_data["amount"]
        if self.debt is None:
            return amount
        if self.debt.balance <= 0:
            raise forms.ValidationError("This debt has no outstanding balance.")
        if amount > self.debt.balance:
            raise forms.ValidationError(
                f"That is more than is owed. The outstanding balance is "
                f"{self.debt.balance}."
            )
        return amount


class BulkRepaymentForm(StyledFormMixin, forms.Form):
    """
    One lump sum spread across a customer's open debts, oldest first.
    Used when a customer pays without naming an invoice.
    """

    amount = forms.DecimalField(
        max_digits=14, decimal_places=2, min_value=Decimal("0.01"),
        label="Total amount received",
    )
    method = forms.ChoiceField(
        choices=Repayment.Method.choices, initial=Repayment.Method.CASH
    )
    external_reference = forms.CharField(max_length=100, required=False,
                                         label="Bank / mobile-money reference")
    note = forms.CharField(required=False, widget=forms.Textarea(attrs={"rows": 2}))
    proof = forms.FileField(
        required=False, widget=MultiFileInput(attrs={"multiple": True}),
        label="Receipt / proof of payment",
    )

    def __init__(self, *args, **kwargs):
        self.account = kwargs.pop("account", None)
        super().__init__(*args, **kwargs)
        if self.account is not None:
            self.fields["amount"].help_text = (
                f"Total outstanding: {self.account.outstanding_balance}. "
                "Payment is applied to the oldest debt first."
            )

    def clean_amount(self):
        amount = self.cleaned_data["amount"]
        if self.account and amount > self.account.outstanding_balance:
            raise forms.ValidationError(
                f"That exceeds the total outstanding debt of "
                f"{self.account.outstanding_balance}."
            )
        return amount


class DebtAdjustForm(StyledFormMixin, forms.ModelForm):
    """Managers may reschedule a due date and add notes - nothing financial."""

    class Meta:
        model = DebtRecord
        fields = ["due_date", "notes"]
        widgets = {
            "due_date": forms.DateInput(attrs={"type": "date"}),
            "notes": forms.Textarea(attrs={"rows": 3}),
        }


class WriteOffForm(StyledFormMixin, forms.Form):
    reason = forms.CharField(
        widget=forms.Textarea(attrs={"rows": 3}),
        label="Reason for write-off",
        help_text="Recorded permanently against the debt and in the audit log.",
    )
    confirm = forms.BooleanField(
        required=True,
        label="I confirm this debt is uncollectable and accept the loss.",
    )


class ReverseRepaymentForm(StyledFormMixin, forms.Form):
    reason = forms.CharField(
        widget=forms.Textarea(attrs={"rows": 3}),
        label="Reason for reversal",
        help_text="The original payment stays visible; it is flagged as reversed.",
    )
    confirm = forms.BooleanField(
        required=True, label="I understand this restores the outstanding balance."
    )


class CreditAccountForm(StyledFormMixin, forms.ModelForm):
    """Admin only - a credit limit is a financial decision."""

    class Meta:
        model = CreditAccount
        fields = ["credit_limit", "default_terms_days", "is_blocked",
                  "block_reason", "notes"]
        widgets = {"notes": forms.Textarea(attrs={"rows": 3})}
        help_texts = {"credit_limit": "Enter 0 for no enforced limit."}

    def clean(self):
        cleaned = super().clean()
        if cleaned.get("is_blocked") and not cleaned.get("block_reason"):
            self.add_error("block_reason", "Give a reason when blocking credit.")
        return cleaned


class DebtFilterForm(forms.Form):
    q = forms.CharField(required=False, label="Search")
    status = forms.ChoiceField(
        required=False,
        choices=[("", "All statuses"), ("OPEN", "Open"), ("PARTIAL", "Partially repaid"),
                 ("OVERDUE", "Overdue only"), ("SETTLED", "Settled"),
                 ("WRITTEN_OFF", "Written off")],
    )
    bucket = forms.ChoiceField(
        required=False, label="Age",
        choices=[("", "Any age"), ("CURRENT", "Not yet due"), ("1-30", "1-30 days"),
                 ("31-60", "31-60 days"), ("61-90", "61-90 days"), ("90+", "Over 90 days")],
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            css = "form-select" if isinstance(field.widget, forms.Select) else "form-control"
            field.widget.attrs.setdefault("class", f"{css} form-control-sm")
        self.fields["q"].widget.attrs["placeholder"] = "Customer, phone or reference..."
