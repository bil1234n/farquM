"""Forms for the yard. Material records, recipes, and the run header."""
from decimal import Decimal, InvalidOperation

from django import forms

from core.scoping import scoped
from inventory.models import Product, Supplier

from .models import ProductionRun, RawMaterial, Recipe


class RawMaterialForm(forms.ModelForm):
    class Meta:
        model = RawMaterial
        fields = [
            "name", "code", "unit", "supplier", "description",
            "unit_cost", "reorder_level", "allow_negative_stock", "is_active",
        ]
        widgets = {"description": forms.Textarea(attrs={"rows": 2})}
        help_texts = {
            "code": "Leave blank to generate one from the name.",
            "reorder_level": "Warn when the store falls to this or below.",
        }

    def __init__(self, *args, **kwargs):
        self.user = kwargs.pop("user", None)
        super().__init__(*args, **kwargs)
        self.fields["code"].required = False
        self.fields["supplier"].queryset = Supplier.objects.filter(is_active=True)
        self.fields["supplier"].empty_label = "No supplier"

        # A manager who may not see cost prices may not set one either. The
        # field is removed rather than disabled: a disabled input still posts
        # on some browsers, and a blank cost would wipe a real one.
        if self.user is not None and not self.user.can_view_financials:
            self.fields.pop("unit_cost", None)
            self.fields.pop("allow_negative_stock", None)

    def _owner_id(self):
        if self.instance and self.instance.pk:
            return self.instance.owner_id
        return getattr(self.user, "pk", None)

    def clean_code(self):
        """
        The per-owner uniqueness rule, with a readable message.

        Django's own validate_unique cannot do this: `owner` is not on the
        form, so it is not part of the instance when that check runs, and the
        database would raise IntegrityError and a 500 page instead.
        """
        code = (self.cleaned_data.get("code") or "").strip()
        if not code:
            return code
        clash = (
            RawMaterial.objects.filter(code__iexact=code)
            .exclude(pk=self.instance.pk if self.instance else None)
            .first()
        )
        if clash is not None:
            raise forms.ValidationError(
                f"That code is already used by '{clash.name}'."
            )
        return code


class MaterialReceiveForm(forms.Form):
    """A delivery arriving at the gate."""

    quantity = forms.DecimalField(
        max_digits=14, decimal_places=3, min_value=Decimal("0.001"),
        label="Quantity received",
    )
    unit_cost = forms.DecimalField(
        max_digits=12, decimal_places=2, min_value=Decimal("0"),
        required=False, label="Cost per unit",
        help_text="Leave blank to keep the current cost.",
    )
    reference = forms.CharField(
        max_length=60, required=False, label="Supplier invoice / delivery note"
    )
    reason = forms.CharField(max_length=255, required=False, label="Note")


class MaterialAdjustForm(forms.Form):
    """
    Waste, a return, or a counted figure.

    One form with a mode rather than three screens, because to the person
    holding the clipboard these are one job: the store does not match the card.
    """

    MODES = (
        ("WASTE", "Spoiled / written off"),
        ("RETURN_OUT", "Returned to supplier"),
        ("RECOUNT", "Counted on the shelf"),
    )

    mode = forms.ChoiceField(choices=MODES, label="What happened")
    quantity = forms.DecimalField(
        max_digits=14, decimal_places=3, min_value=Decimal("0"),
        label="Quantity",
        help_text="For a count, the figure you actually measured.",
    )
    reason = forms.CharField(max_length=255, required=False, label="Reason")

    def clean(self):
        cleaned = super().clean()
        mode = cleaned.get("mode")
        quantity = cleaned.get("quantity")
        # Zero is a real statement for a count ("the bay is empty") and
        # meaningless for a write-off.
        if mode != "RECOUNT" and quantity is not None and quantity <= 0:
            self.add_error("quantity", "Enter a quantity greater than zero.")
        return cleaned


class RecipeForm(forms.ModelForm):
    class Meta:
        model = Recipe
        fields = ["output_quantity", "notes", "is_active"]
        widgets = {"notes": forms.Textarea(attrs={"rows": 2})}
        labels = {"output_quantity": "One batch makes"}
        help_texts = {
            "output_quantity": "How many finished units the materials below "
                               "produce. Set it to 1 for a per-unit recipe.",
        }

    def clean_output_quantity(self):
        value = self.cleaned_data.get("output_quantity") or 0
        if value < 1:
            raise forms.ValidationError("A batch must make at least one unit.")
        return value


class ProductionRunForm(forms.ModelForm):
    """
    The header. Material lines arrive as parallel arrays and are parsed by the
    view, the same way the till parses a cart.
    """

    class Meta:
        model = ProductionRun
        fields = ["product", "quantity_produced", "quantity_rejected",
                  "produced_on", "notes"]
        widgets = {
            "produced_on": forms.DateInput(attrs={"type": "date"}),
            "notes": forms.Textarea(attrs={"rows": 2}),
        }
        labels = {
            "quantity_produced": "Good units produced",
            "quantity_rejected": "Rejected / broken",
            "produced_on": "Date produced",
        }
        help_texts = {
            "quantity_rejected": "Units that failed. The materials for them "
                                 "were still used, so they carry their share "
                                 "of the cost.",
        }

    def __init__(self, *args, **kwargs):
        self.user = kwargs.pop("user", None)
        super().__init__(*args, **kwargs)
        if self.user is not None:
            self.fields["product"].queryset = scoped(
                Product.objects.active(), self.user
            ).order_by("name")
        self.fields["product"].empty_label = "Select a product"

    def clean_quantity_produced(self):
        value = self.cleaned_data.get("quantity_produced") or 0
        if value < 1:
            raise forms.ValidationError("A run must produce at least one unit.")
        return value


class ReversalForm(forms.Form):
    reason = forms.CharField(
        max_length=255,
        label="Why is this run being reversed?",
        help_text="Kept on the record beside the original batch.",
    )

    def clean_reason(self):
        reason = (self.cleaned_data.get("reason") or "").strip()
        if not reason:
            raise forms.ValidationError("Give a reason for reversing this run.")
        return reason


def parse_material_lines(request, user):
    """
    Turn the POSTed parallel arrays into the list `record_production` wants.

    Returns (lines, errors). Scoped, so a hand-edited form naming another
    manager's material lands in "not found" rather than consuming their store.
    """
    ids = request.POST.getlist("material_id[]")
    quantities = request.POST.getlist("quantity[]")
    expected = request.POST.getlist("expected[]")

    lines, errors = [], []
    if not ids:
        return lines, ["Record at least one material used in this run."]

    store = {
        m.pk: m
        for m in scoped(RawMaterial.objects.active(), user).filter(
            pk__in=[i for i in ids if i]
        )
    }

    for index, raw_id in enumerate(ids):
        if not raw_id:
            continue
        material = store.get(int(raw_id)) if raw_id.isdigit() else None
        if material is None:
            errors.append("One of the materials is not in your store.")
            continue

        try:
            quantity = Decimal(quantities[index] or "0")
        except (IndexError, InvalidOperation):
            quantity = Decimal("0")
        if quantity <= 0:
            errors.append(f"Enter how much '{material.name}' this run used.")
            continue

        try:
            want = Decimal(expected[index]) if expected[index] else None
        except (IndexError, InvalidOperation):
            want = None

        lines.append(
            {"material": material, "quantity": quantity, "expected_quantity": want}
        )

    return lines, errors
