"""
Forms behind the Settings hub.

The permission GRID is deliberately not a Django form. A form with 40-odd
dynamically generated BooleanFields renders as a flat list and fights every
attempt to group it, tint the sensitive rows, or show which boxes came from
the role rather than from this person. So the grid is plain checkboxes named
`perm`, rendered by the template from core.permissions.CATALOG and read back
with `request.POST.getlist("perm")`.

These forms cover the fields around that grid, where Django's validation and
error rendering genuinely earn their place.
"""
from django import forms

from accounts.models import DataScope, RoleCode, RoleDefinition, User
from core.models import SystemSetting

INPUT = "form-control"
SELECT = "form-select"


class StyledMixin:
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            widget = field.widget
            if isinstance(widget, forms.CheckboxInput):
                widget.attrs.setdefault("class", "form-check-input")
            elif isinstance(widget, (forms.Select, forms.SelectMultiple)):
                widget.attrs.setdefault("class", SELECT)
            else:
                widget.attrs.setdefault("class", INPUT)


class UserAccessForm(StyledMixin, forms.ModelForm):
    """
    The non-grid half of one person's access: which role they start from,
    whose stock they sell, and how far they can see.
    """

    class Meta:
        model = User
        fields = ["role", "manager", "data_scope_override"]

    def __init__(self, *args, **kwargs):
        self.editor = kwargs.pop("editor", None)
        super().__init__(*args, **kwargs)

        self.fields["role"] = forms.ChoiceField(
            choices=RoleDefinition.objects.assignable().as_choices(),
            label="Role",
            initial=self.instance.role,
            widget=forms.Select(attrs={"class": SELECT, "id": "id_role"}),
            help_text="Ticking or unticking a box below records a difference "
                      "from this role, not a change to it.",
        )

        managers = User.objects.filter(is_active=True).exclude(role=RoleCode.SALES)
        if self.instance.pk:
            managers = managers.exclude(pk=self.instance.pk)
        self.fields["manager"].queryset = managers.order_by("username")
        self.fields["manager"].required = False
        self.fields["manager"].empty_label = "Nobody - works independently"
        self.fields["manager"].label = "Sells stock belonging to"
        self.fields["manager"].help_text = (
            "A sales user needs this: it is the shelf they sell from. Their "
            "own sales, customers and debts still belong to them."
        )

        self.fields["data_scope_override"].label = "Data scope"
        self.fields["data_scope_override"].required = False
        role = self.instance.role_definition
        inherited = dict(DataScope.choices).get(
            role.data_scope if role else DataScope.OWN, "Own records only"
        )
        self.fields["data_scope_override"].choices = [
            ("", f"Use the role's setting ({inherited})")
        ] + list(DataScope.choices)

    def clean_manager(self):
        manager = self.cleaned_data.get("manager")
        if manager and manager.pk == self.instance.pk:
            raise forms.ValidationError("A user cannot sell their own stock via themselves.")
        if manager and manager.manager_id == self.instance.pk:
            raise forms.ValidationError(
                f"{manager.display_name} already reports to this user."
            )
        return manager

    def clean_role(self):
        role = self.cleaned_data["role"]
        if not RoleDefinition.objects.filter(code=role, is_active=True).exists():
            raise forms.ValidationError("That role no longer exists.")
        if (
            self.instance.role == RoleCode.ADMIN
            and role != RoleCode.ADMIN
            and not User.objects.admins()
            .filter(is_active=True)
            .exclude(pk=self.instance.pk)
            .exists()
        ):
            raise forms.ValidationError(
                "This is the only active administrator. Promote another user first."
            )
        return role


class RoleForm(StyledMixin, forms.ModelForm):
    """Create or edit a role. Permissions come from the grid, not from here."""

    class Meta:
        model = RoleDefinition
        fields = ["code", "name", "description", "data_scope", "rank", "is_active"]
        widgets = {"description": forms.Textarea(attrs={"rows": 2})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["code"].help_text = (
            "Short uppercase identifier used in the database and the API. "
            "Cannot be changed once the role exists."
        )
        self.fields["rank"].help_text = (
            "Sort order in every role list. Lower is more senior."
        )
        self.fields["data_scope"].help_text = (
            "OWN: only their own records. TEAM: theirs plus anyone reporting "
            "to them. ALL: the whole business."
        )
        if self.instance.pk:
            # The code is written into every user row that holds this role.
            # Editing it would orphan all of them at once.
            self.fields["code"].disabled = True
            if self.instance.is_system:
                self.fields["is_active"].disabled = True
                self.fields["is_active"].help_text = (
                    "Built-in roles cannot be switched off."
                )

    def clean_code(self):
        code = (self.cleaned_data.get("code") or "").strip().upper().replace(" ", "_")
        if self.instance.pk:
            return self.instance.code
        if not code:
            raise forms.ValidationError("A code is required.")
        if not code.replace("_", "").isalnum():
            raise forms.ValidationError("Use letters, digits and underscores only.")
        if RoleDefinition.objects.filter(code=code).exists():
            raise forms.ValidationError("A role with that code already exists.")
        return code

    def clean_data_scope(self):
        scope = self.cleaned_data["data_scope"]
        if self.instance.pk and self.instance.code == RoleCode.ADMIN and scope != DataScope.ALL:
            raise forms.ValidationError(
                "The Administrator role must be able to see every record."
            )
        return scope


class SystemSettingForm(StyledMixin, forms.ModelForm):
    class Meta:
        model = SystemSetting
        fields = [
            "business_name",
            "business_phone",
            "business_email",
            "business_address",
            "currency_symbol",
            "default_credit_due_days",
            "low_stock_threshold",
            "allow_self_registration",
            "require_credit_approval",
        ]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        from django.conf import settings as django_settings

        # Placeholders show what the app falls back to when a field is blank,
        # so an administrator can see the current effective value without
        # having to guess whether "empty" means "empty" or "inherited".
        fallbacks = {
            "business_name": django_settings.BUSINESS_NAME,
            "business_phone": django_settings.BUSINESS_PHONE,
            "business_address": django_settings.BUSINESS_ADDRESS,
            "currency_symbol": django_settings.CURRENCY_SYMBOL,
        }
        for field, value in fallbacks.items():
            if value:
                self.fields[field].widget.attrs["placeholder"] = value

    def save(self, commit=True):
        obj = super().save(commit=commit)
        if commit:
            # The money filter caches the symbol for five minutes; without
            # this an administrator changes the currency and nothing appears
            # to happen.
            from django.core.cache import cache

            from core.templatetags.core_extras import CURRENCY_CACHE_KEY

            cache.delete(CURRENCY_CACHE_KEY)
        return obj
