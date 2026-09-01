from decimal import Decimal

from django import forms
from django.utils import timezone

from accounts.forms import StyledFormMixin
from core.forms import ReceiptField
from core.scoping import scoped
from core.utils import ZERO, default_due_date

from .models import Customer, PaymentMethod, Receipt, Transaction


class CustomerForm(StyledFormMixin, forms.ModelForm):
    class Meta:
        model = Customer
        fields = [
            "name", "phone", "alternate_phone", "email", "address",
            "customer_type", "is_credit_approved", "is_active", "notes",
        ]
        widgets = {
            "address": forms.Textarea(attrs={"rows": 2}),
            "notes": forms.Textarea(attrs={"rows": 2}),
        }

    def __init__(self, *args, **kwargs):
        self.user = kwargs.pop("user", None)
        super().__init__(*args, **kwargs)
        # Approving a customer for credit decides how deep they may go, so it
        # rides with the same permission that sets credit limits rather than
        # with "may edit a customer".
        if self.user is not None and not self.user.has_access("credit.limits"):
            self.fields.pop("is_credit_approved", None)
            self.fields.pop("is_active", None)

    def clean_phone(self):
        """
        Unique within THIS user's customer list, not globally.

        The same person genuinely can be a customer of two different managers.
        Checking globally would tell manager B their number is taken by a
        record they cannot see, open, or do anything about.
        """
        phone = (self.cleaned_data.get("phone") or "").strip()
        if not phone:
            raise forms.ValidationError("A phone number is required.")

        owner_id = (
            self.instance.owner_id
            if self.instance and self.instance.pk
            else getattr(self.user, "pk", None)
        )
        qs = Customer.objects.filter(owner_id=owner_id, phone__iexact=phone)
        if self.instance.pk:
            qs = qs.exclude(pk=self.instance.pk)
        if qs.exists():
            raise forms.ValidationError(
                "You already have a customer registered with this phone number."
            )
        return phone


class SaleHeaderForm(StyledFormMixin, forms.Form):
    """
    The non-cart half of the point-of-sale screen.
    Line items arrive as a parallel POST array and are parsed in the view.
    """

    customer = forms.ModelChoiceField(
        queryset=Customer.objects.none(),
        required=False,
        empty_label="Walk-in customer (cash only)",
        help_text="Required if any balance will be left unpaid.",
    )
    payment_method = forms.ChoiceField(
        choices=PaymentMethod.choices, initial=PaymentMethod.CASH
    )
    amount_paid = forms.DecimalField(
        max_digits=14, decimal_places=2, min_value=Decimal("0"),
        initial=Decimal("0.00"), label="Amount paid now",
        help_text="Enter 0 for a full 'Pay Later' credit sale.",
    )
    discount_amount = forms.DecimalField(
        max_digits=14, decimal_places=2, min_value=Decimal("0"),
        required=False, initial=Decimal("0.00"), label="Discount",
    )
    tax_amount = forms.DecimalField(
        max_digits=14, decimal_places=2, min_value=Decimal("0"),
        required=False, initial=Decimal("0.00"), label="Tax",
    )
    due_date = forms.DateField(
        required=False,
        widget=forms.DateInput(attrs={"type": "date"}),
        help_text="When the balance falls due. Defaults to 30 days.",
    )
    # ReceiptField, not FileField. A plain FileField behind a `multiple`
    # widget receives a LIST and dies with "No file was submitted. Check the
    # encoding type on the form." - see core/forms.py for the full story.
    receipt = ReceiptField(
        help_text="Optional. Photograph the paper receipt, bank slip or mobile-money "
                  "confirmation. Images or PDF, up to 5 MB each.",
    )
    receipt_kind = forms.ChoiceField(
        required=False,
        choices=Receipt.Kind.choices,
        initial=Receipt.Kind.SALE,
        label="Attachment type",
    )
    notes = forms.CharField(
        required=False, widget=forms.Textarea(attrs={"rows": 2}), label="Notes"
    )

    def __init__(self, *args, **kwargs):
        self.user = kwargs.pop("user", None)
        # Default True so the API and any other caller that has not been
        # updated keeps its current behaviour; the server-side rule in
        # sales.services.create_sale is what actually enforces these.
        self.can_credit = kwargs.pop("can_credit", True)
        self.can_discount = kwargs.pop("can_discount", True)
        super().__init__(*args, **kwargs)
        # Only this user's customers are selectable. Rendering the full list
        # would leak every customer name and phone number in the business
        # through a dropdown, which is the easiest kind of leak to miss.
        self.fields["customer"].queryset = scoped(
            Customer.objects.active(), self.user
        ).order_by("name")
        self.fields["due_date"].initial = default_due_date()

        if not self.can_discount:
            # Removed rather than disabled: a disabled input submits nothing,
            # but a hidden one can still be re-enabled from the console.
            self.fields.pop("discount_amount", None)

        if not self.can_credit:
            self.fields.pop("due_date", None)
            self.fields["amount_paid"].help_text = (
                "You must collect the full amount - selling on credit is not "
                "part of your access."
            )
            self.fields["customer"].help_text = "Optional for a cash sale."

    def clean_due_date(self):
        due = self.cleaned_data.get("due_date")
        if due and due < timezone.localdate():
            raise forms.ValidationError("The due date cannot be in the past.")
        return due

    def clean(self):
        """
        A bank transfer, cheque or mobile-money payment leaves no cash in the
        till, so proof matters more. We warn rather than block, because the
        clerk may not have the slip in hand at the counter - blocking the sale
        would just push them to record it as cash, which is worse.
        """
        cleaned = super().clean()
        if not self.can_discount:
            cleaned["discount_amount"] = ZERO
        method = cleaned.get("payment_method")
        paid = cleaned.get("amount_paid") or 0
        # Read the cleaned value, not self.files - cleaned_data is now a list
        # of validated uploads, so a file that failed the size check does not
        # count as proof.
        has_proof = bool(cleaned.get("receipt"))

        if method in {"BANK", "CHEQUE", "MOBILE"} and paid > 0 and not has_proof:
            self.proof_warning = (
                f"No proof attached for a {dict(PaymentMethod.choices)[method]} payment. "
                "You can add it later from the transaction page."
            )
        return cleaned


class ReceiptUploadForm(StyledFormMixin, forms.ModelForm):
    class Meta:
        model = Receipt
        fields = ["file", "kind", "caption"]
        labels = {"file": "Receipt file (image or PDF)"}


class VoidTransactionForm(StyledFormMixin, forms.Form):
    reason = forms.CharField(
        widget=forms.Textarea(attrs={"rows": 3}),
        label="Reason for voiding",
        help_text="Recorded permanently in the audit log.",
    )
    confirm = forms.BooleanField(
        label="I understand this reverses stock and cancels any linked debt.",
        required=True,
    )


class TransactionFilterForm(forms.Form):
    q = forms.CharField(required=False, label="Search")
    status = forms.ChoiceField(
        required=False,
        choices=[("", "All statuses"), ("PAID", "Paid"),
                 ("PARTIAL", "Partially paid"), ("UNPAID", "Unpaid (Credit)"),
                 ("REFUNDED", "Voided / Refunded")],
    )
    date_from = forms.DateField(required=False, widget=forms.DateInput(attrs={"type": "date"}))
    date_to = forms.DateField(required=False, widget=forms.DateInput(attrs={"type": "date"}))
    seller = forms.ChoiceField(required=False, choices=[])

    def __init__(self, *args, **kwargs):
        sellers = kwargs.pop("sellers", None)
        super().__init__(*args, **kwargs)
        choices = [("", "All staff")]
        if sellers is not None:
            choices += [(str(u.pk), u.display_name) for u in sellers]
        self.fields["seller"].choices = choices
        for field in self.fields.values():
            css = "form-select" if isinstance(field.widget, forms.Select) else "form-control"
            field.widget.attrs.setdefault("class", f"{css} form-control-sm")
        self.fields["q"].widget.attrs["placeholder"] = "Reference, customer, phone..."
