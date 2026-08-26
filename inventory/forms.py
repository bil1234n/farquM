from django import forms

from accounts.forms import StyledFormMixin

from .models import Category, Product, Supplier


class CategoryForm(StyledFormMixin, forms.ModelForm):
    class Meta:
        model = Category
        fields = ["name", "description", "is_active"]
        widgets = {"description": forms.Textarea(attrs={"rows": 2})}


class SupplierForm(StyledFormMixin, forms.ModelForm):
    class Meta:
        model = Supplier
        fields = [
            "name", "contact_person", "phone", "email",
            "address", "is_active", "notes",
        ]
        widgets = {
            "address": forms.Textarea(attrs={"rows": 2}),
            "notes": forms.Textarea(attrs={"rows": 2}),
        }


class ProductForm(StyledFormMixin, forms.ModelForm):
    """
    Category and Supplier are free-text fields with autocomplete rather than
    dropdowns. Typing a name that does not exist creates it on the fly, so a
    clerk adding stock is never blocked by a missing lookup record.

    Matching is case-insensitive, which stops "Drinks" and "drinks" becoming
    two separate categories.
    """

    category_name = forms.CharField(
        required=False,
        label="Category",
        help_text="Type to search, or enter a new name to create it.",
        widget=forms.TextInput(attrs={
            "list": "categoryOptions",
            "autocomplete": "off",
            "placeholder": "e.g. Beverages",
        }),
    )
    supplier_name = forms.CharField(
        required=False,
        label="Supplier",
        help_text="Type to search, or enter a new name to create it.",
        widget=forms.TextInput(attrs={
            "list": "supplierOptions",
            "autocomplete": "off",
            "placeholder": "e.g. Addis Wholesale PLC",
        }),
    )

    field_order = [
        "name", "sku", "barcode", "category_name", "supplier_name", "unit",
        "description", "cost_price", "selling_price", "low_stock_threshold",
        "allow_negative_stock", "image", "is_active",
    ]

    class Meta:
        model = Product
        fields = [
            "name", "sku", "barcode", "unit", "description",
            "cost_price", "selling_price", "low_stock_threshold",
            "allow_negative_stock", "image", "is_active",
        ]
        widgets = {"description": forms.Textarea(attrs={"rows": 3})}
        help_texts = {
            "sku": "Leave blank to auto-generate.",
            "barcode": "Optional. Must be unique within your own products.",
        }

    def __init__(self, *args, **kwargs):
        self.user = kwargs.pop("user", None)
        super().__init__(*args, **kwargs)

        self.fields["sku"].required = False

        # Pre-fill the text boxes when editing an existing product.
        if self.instance and self.instance.pk:
            if self.instance.category_id:
                self.fields["category_name"].initial = self.instance.category.name
            if self.instance.supplier_id:
                self.fields["supplier_name"].initial = self.instance.supplier.name

        # Autocomplete sources rendered as <datalist> in the template.
        self.category_options = list(
            Category.objects.filter(is_active=True).values_list("name", flat=True)
        )
        self.supplier_options = list(
            Supplier.objects.filter(is_active=True).values_list("name", flat=True)
        )

        # RBAC at the form layer: a Manager never even sees cost price.
        if self.user is not None and not self.user.can_view_financials:
            self.fields.pop("cost_price", None)
            self.fields.pop("allow_negative_stock", None)

    # -- Cleaning ------------------------------------------------------------
    def clean_barcode(self):
        return (self.cleaned_data.get("barcode") or "").strip() or None

    def _owner_id(self):
        """
        Who will own this product once saved.

        On an edit that is the existing owner; on a create it is whoever is
        filling the form in. Uniqueness has to be tested against that person's
        catalogue, not the whole database.
        """
        if self.instance and self.instance.pk:
            return self.instance.owner_id
        return getattr(self.user, "pk", None)

    def _check_unique_within_owner(self, field, value):
        """
        Enforce the per-owner unique constraint with a readable message.

        Without this the database raises IntegrityError on save and the user
        sees a 500 page. Django's own uniqueness validation cannot help here:
        `owner` is excluded from the form, so it is not part of the instance
        when validate_unique() runs.
        """
        if not value:
            return value
        clash = Product.objects.filter(
            owner_id=self._owner_id(), **{f"{field}__iexact": value}
        ).exclude(pk=self.instance.pk if self.instance else None)
        if clash.exists():
            raise forms.ValidationError(
                f"You already have a product with this {field}: "
                f"'{clash.first().name}'."
            )
        return value

    def clean_sku(self):
        return self._check_unique_within_owner(
            "sku", (self.cleaned_data.get("sku") or "").strip()
        )

    def clean_category_name(self):
        return (self.cleaned_data.get("category_name") or "").strip()

    def clean_supplier_name(self):
        return (self.cleaned_data.get("supplier_name") or "").strip()

    def clean(self):
        cleaned = super().clean()
        cost = cleaned.get("cost_price")
        price = cleaned.get("selling_price")
        if cost is not None and price is not None and price < cost:
            self.add_error(
                "selling_price",
                "Selling price is below cost price - this product would sell at a loss.",
            )
        barcode = cleaned.get("barcode")
        if barcode:
            try:
                self._check_unique_within_owner("barcode", barcode)
            except forms.ValidationError as exc:
                self.add_error("barcode", exc)
        return cleaned

    # -- Resolve free text to real rows -------------------------------------
    def _resolve_category(self):
        name = self.cleaned_data.get("category_name", "")
        if not name:
            return None
        existing = Category.objects.filter(name__iexact=name).first()
        if existing:
            return existing
        self._created_category = True
        return Category.objects.create(name=name)

    def _resolve_supplier(self):
        name = self.cleaned_data.get("supplier_name", "")
        if not name:
            return None
        existing = Supplier.objects.filter(name__iexact=name).first()
        if existing:
            return existing
        self._created_supplier = True
        return Supplier.objects.create(name=name)

    def save(self, commit=True):
        product = super().save(commit=False)
        product.category = self._resolve_category()
        product.supplier = self._resolve_supplier()
        if commit:
            product.save()
            self.save_m2m()
        return product

    @property
    def created_lookups(self):
        """Names of any Category/Supplier rows this submission created."""
        created = []
        if getattr(self, "_created_category", False):
            created.append(f"category '{self.cleaned_data.get('category_name')}'")
        if getattr(self, "_created_supplier", False):
            created.append(f"supplier '{self.cleaned_data.get('supplier_name')}'")
        return created


class RestockForm(StyledFormMixin, forms.Form):
    quantity = forms.IntegerField(min_value=1, label="Quantity received")
    unit_cost = forms.DecimalField(
        max_digits=12, decimal_places=2, required=False, label="Unit cost",
        help_text="Optional. If given, the product cost price is updated.",
    )
    reference = forms.CharField(
        max_length=60, required=False, label="Supplier invoice / reference"
    )
    reason = forms.CharField(max_length=255, required=False, label="Note")

    def __init__(self, *args, **kwargs):
        self.user = kwargs.pop("user", None)
        super().__init__(*args, **kwargs)
        if self.user is not None and not self.user.can_view_financials:
            self.fields.pop("unit_cost", None)


class StockAdjustForm(StyledFormMixin, forms.Form):
    ADJUST_MODE = (
        ("SET", "Set to counted quantity (stock-take)"),
        ("DAMAGE", "Write off damaged / lost stock"),
        ("RETURN_IN", "Customer return into stock"),
    )
    mode = forms.ChoiceField(choices=ADJUST_MODE, label="Adjustment type")
    quantity = forms.IntegerField(
        min_value=0, label="Quantity",
        help_text="For a stock-take this is the new total. Otherwise it is the amount moved.",
    )
    reason = forms.CharField(
        max_length=255, label="Reason",
        help_text="Required - this is written to the permanent ledger.",
    )

    def clean(self):
        cleaned = super().clean()
        mode = cleaned.get("mode")
        qty = cleaned.get("quantity")
        if mode in {"DAMAGE", "RETURN_IN"} and (qty is None or qty <= 0):
            self.add_error("quantity", "Quantity must be greater than zero.")
        return cleaned


class ProductFilterForm(forms.Form):
    q = forms.CharField(required=False, label="Search")
    category = forms.ModelChoiceField(
        queryset=Category.objects.filter(is_active=True),
        required=False, empty_label="All categories",
    )
    stock = forms.ChoiceField(
        required=False,
        choices=[("", "All stock levels"), ("low", "Low stock"),
                 ("out", "Out of stock"), ("ok", "In stock")],
    )
    status = forms.ChoiceField(
        required=False,
        choices=[("", "Active & inactive"), ("active", "Active only"),
                 ("inactive", "Inactive only")],
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for name, field in self.fields.items():
            css = "form-select" if isinstance(field.widget, forms.Select) else "form-control"
            field.widget.attrs.setdefault("class", f"{css} form-control-sm")
        self.fields["q"].widget.attrs["placeholder"] = "Name, SKU or barcode..."
