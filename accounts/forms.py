from django import forms
from django.contrib.auth.forms import AuthenticationForm, UserCreationForm
from django.contrib.auth.password_validation import validate_password

from .models import Role, User
from .registration import available_roles

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

    remove_avatar = forms.BooleanField(
        required=False,
        label="Remove my current photo",
        help_text="Tick to go back to your initials.",
    )

    class Meta:
        model = User
        fields = ["first_name", "last_name", "email", "phone", "avatar"]
        labels = {"avatar": "Profile photo"}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["avatar"].required = False
        self.fields["avatar"].widget.attrs["accept"] = "image/*"
        if not self.instance.pk or not self.instance.avatar:
            # Nothing to remove, so do not offer a checkbox that does nothing.
            self.fields.pop("remove_avatar", None)

    def save(self, commit=True):
        user = super().save(commit=False)
        if self.cleaned_data.get("remove_avatar"):
            # Clear the field first, then delete the blob. Doing it the other
            # way round leaves the row pointing at a file that no longer
            # exists if the delete succeeds and the save then fails.
            old = user.avatar
            user.avatar = None
            if commit:
                user.save()
                try:
                    old.delete(save=False)
                except Exception:
                    # Losing an orphaned blob is not worth failing the request.
                    pass
            return user
        if commit:
            user.save()
        return user


class RegisterForm(StyledFormMixin, forms.Form):
    """
    Self-service staff registration.

    The passcode field is what makes this safe to expose publicly - see
    accounts/registration.py for the threat model. This form only collects and
    shape-checks the input; the passcode itself is verified in the service
    layer, which also does the rate limiting and audit logging.
    """

    username = forms.CharField(
        max_length=150,
        widget=forms.TextInput(attrs={"autofocus": True, "autocomplete": "username"}),
        help_text="Used to sign in. Letters, digits and @/./+/-/_ only.",
    )
    first_name = forms.CharField(max_length=150, required=False)
    last_name = forms.CharField(max_length=150, required=False)
    email = forms.EmailField(required=False)
    phone = forms.CharField(max_length=30, required=False)
    avatar = forms.ImageField(
        required=False,
        label="Profile photo",
        widget=forms.ClearableFileInput(attrs={"accept": "image/*"}),
    )
    role = forms.ChoiceField(
        choices=[],
        label="I am registering as",
        help_text="You need the passcode for the role you pick.",
    )
    passcode = forms.CharField(
        widget=forms.PasswordInput(attrs={"autocomplete": "one-time-code"}),
        label="Registration passcode",
        help_text="Ask the business owner for the code for your role.",
    )
    password1 = forms.CharField(
        label="Password",
        widget=forms.PasswordInput(attrs={"autocomplete": "new-password"}),
    )
    password2 = forms.CharField(
        label="Confirm password",
        widget=forms.PasswordInput(attrs={"autocomplete": "new-password"}),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Only roles whose passcode is actually configured are offered. A role
        # with a blank passcode is not merely unusable - listing it would tell
        # a stranger the role exists and invite guessing.
        self.fields["role"].choices = available_roles()

    def clean_username(self):
        username = (self.cleaned_data["username"] or "").strip()
        if User.objects.filter(username__iexact=username).exists():
            raise forms.ValidationError("That username is already taken.")
        return username

    def clean(self):
        cleaned = super().clean()
        p1, p2 = cleaned.get("password1"), cleaned.get("password2")
        if p1 and p2 and p1 != p2:
            self.add_error("password2", "The two passwords do not match.")
        elif p1:
            # Run Django's configured validators so the same minimum-length
            # and common-password rules apply here as everywhere else.
            try:
                validate_password(p1)
            except forms.ValidationError as exc:
                self.add_error("password1", exc)
        return cleaned
