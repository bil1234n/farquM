from django import forms
from django.contrib.auth.forms import AuthenticationForm, UserCreationForm

from .models import Role, User

INPUT = "form-control"
SELECT = "form-select"


class StyledFormMixin:
    """Applies Bootstrap classes without needing per-field boilerplate."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            widget = field.widget
            if isinstance(widget, (forms.Select, forms.SelectMultiple)):
                widget.attrs.setdefault("class", SELECT)
            elif isinstance(widget, forms.CheckboxInput):
                widget.attrs.setdefault("class", "form-check-input")
            elif isinstance(widget, forms.FileInput):
                widget.attrs.setdefault("class", INPUT)
            else:
                widget.attrs.setdefault("class", INPUT)
                if field.label:
                    widget.attrs.setdefault("placeholder", field.label)


class LoginForm(StyledFormMixin, AuthenticationForm):
    username = forms.CharField(
        widget=forms.TextInput(attrs={"autofocus": True, "autocomplete": "username"})
    )
    password = forms.CharField(
        widget=forms.PasswordInput(attrs={"autocomplete": "current-password"})
    )

    error_messages = {
        "invalid_login": "Incorrect username or password.",
        "inactive": "This account has been deactivated. Contact an administrator.",
    }


class UserCreateForm(StyledFormMixin, UserCreationForm):
    class Meta:
        model = User
        fields = [
            "username",
            "first_name",
            "last_name",
            "email",
            "phone",
            "employee_id",
            "role",
            "is_active",
            "notes",
        ]
        widgets = {"notes": forms.Textarea(attrs={"rows": 2})}

    def clean_role(self):
        role = self.cleaned_data["role"]
        if role not in Role.values:
            raise forms.ValidationError("Invalid role.")
        return role


class UserUpdateForm(StyledFormMixin, forms.ModelForm):
    """Password is changed separately - never on the profile edit form."""

    class Meta:
        model = User
        fields = [
            "username",
            "first_name",
            "last_name",
            "email",
            "phone",
            "employee_id",
            "role",
            "is_active",
            "notes",
        ]
        widgets = {"notes": forms.Textarea(attrs={"rows": 2})}

    def __init__(self, *args, **kwargs):
        self.editing_user = kwargs.pop("editing_user", None)
        super().__init__(*args, **kwargs)

    def clean_is_active(self):
        active = self.cleaned_data["is_active"]
        if not active and self.editing_user and self.instance.pk == self.editing_user.pk:
            raise forms.ValidationError("You cannot deactivate your own account.")
        return active

    def clean_role(self):
        role = self.cleaned_data["role"]
        # Guard: never let the last remaining admin demote themselves.
        if (
            self.instance.pk
            and self.instance.role == Role.ADMIN
            and role != Role.ADMIN
            and User.objects.admins().filter(is_active=True).exclude(pk=self.instance.pk).count() == 0
        ):
            raise forms.ValidationError(
                "This is the only active administrator. Promote another user first."
            )
        return role


class AdminPasswordResetForm(StyledFormMixin, forms.Form):
    """Admin sets a temporary password for another user."""

    new_password1 = forms.CharField(
        label="New password", widget=forms.PasswordInput, min_length=8
    )
    new_password2 = forms.CharField(
        label="Confirm password", widget=forms.PasswordInput, min_length=8
    )
    force_change = forms.BooleanField(
        label="Require the user to change it at next login",
        required=False,
        initial=True,
    )

    def clean(self):
        cleaned = super().clean()
        if cleaned.get("new_password1") != cleaned.get("new_password2"):
            raise forms.ValidationError("The two passwords do not match.")
        return cleaned


class SelfProfileForm(StyledFormMixin, forms.ModelForm):
    """What a user may edit about themselves - notably NOT role or is_active."""

    class Meta:
        model = User
        fields = ["first_name", "last_name", "email", "phone"]
