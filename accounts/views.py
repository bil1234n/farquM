from django.conf import settings
from django.contrib import messages
from django.contrib.auth import login as auth_login, update_session_auth_hash
from django.contrib.auth.forms import PasswordChangeForm
from django.contrib.auth.views import LoginView, LogoutView
from django.db.models import Q
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse_lazy
from django.views.generic import CreateView, DetailView, ListView, UpdateView

from core.mixins import AdminRequiredMixin, StaffRequiredMixin
from core.middleware import client_ip

from .forms import (
    AdminPasswordResetForm,
    LoginForm,
    RegisterForm,
    SelfProfileForm,
    UserCreateForm,
    UserUpdateForm,
)
from .models import AuditAction, AuditLog, Role, User
from .registration import RegistrationError, register_user, registration_open
from .services import diff_instance, log_action


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------
class AppLoginView(LoginView):
    template_name = "registration/login.html"
    authentication_form = LoginForm
    redirect_authenticated_user = True

    def form_valid(self, form):
        response = super().form_valid(form)
        self.request.user.touch()
        log_action(
            AuditAction.LOGIN,
            instance=self.request.user,
            description=f"{self.request.user.username} signed in.",
            user=self.request.user,
            request=self.request,
        )
        messages.success(self.request, f"Welcome back, {self.request.user.display_name}.")
        return response

    def form_invalid(self, form):
        log_action(
            AuditAction.LOGIN_FAILED,
            description=f"Failed login for '{form.data.get('username', '')[:150]}'.",
            user=None,
            request=self.request,
        )
        return super().form_invalid(form)


class AppLogoutView(LogoutView):
    next_page = reverse_lazy("accounts:login")

    def dispatch(self, request, *args, **kwargs):
        if request.user.is_authenticated:
            log_action(
                AuditAction.LOGOUT,
                instance=request.user,
                description=f"{request.user.username} signed out.",
                user=request.user,
                request=request,
            )
        return super().dispatch(request, *args, **kwargs)


def register(request):
    """
    Self-service staff registration, gated by a per-role passcode.

    Replaces `manage.py createsuperuser` for onboarding. All the security
    lives in accounts/registration.py - rate limiting, constant-time passcode
    comparison, audit logging. This view is deliberately thin so none of that
    can be bypassed by adding a second entry point later.
    """
    if request.user.is_authenticated:
        return redirect("reports:dashboard")

    if not registration_open():
        # No passcodes configured means registration is switched off. Say so
        # plainly rather than showing a form that can never succeed.
        return render(request, "registration/register_closed.html", status=403)

    form = RegisterForm(request.POST or None, request.FILES or None)

    if request.method == "POST" and form.is_valid():
        try:
            user = register_user(
                username=form.cleaned_data["username"],
                password=form.cleaned_data["password1"],
                role=form.cleaned_data["role"],
                passcode=form.cleaned_data["passcode"],
                first_name=form.cleaned_data.get("first_name", ""),
                last_name=form.cleaned_data.get("last_name", ""),
                email=form.cleaned_data.get("email", ""),
                phone=form.cleaned_data.get("phone", ""),
                avatar=form.cleaned_data.get("avatar"),
                ip=client_ip(request),
                request=request,
            )
        except RegistrationError as exc:
            # Attach to the passcode field when that is what failed, so the
            # error appears next to the input the user has to fix.
            form.add_error(
                "passcode" if "passcode" in str(exc).lower() else None, str(exc)
            )
        else:
            auth_login(request, user)
            user.touch()
            messages.success(
                request,
                f"Welcome, {user.display_name}. Your "
                f"{user.get_role_display().lower()} account is ready.",
            )
            return redirect("reports:dashboard")

    return render(
        request,
        "registration/register.html",
        {"form": form, "business_name": settings.BUSINESS_NAME},
    )


# ---------------------------------------------------------------------------
# User management - ADMIN ONLY
# ---------------------------------------------------------------------------
class UserListView(AdminRequiredMixin, ListView):
    model = User
    template_name = "accounts/user_list.html"
    context_object_name = "users"
    paginate_by = 25

    def get_queryset(self):
        qs = User.objects.all().order_by("-is_active", "role", "username")
        q = self.request.GET.get("q", "").strip()
        role = self.request.GET.get("role", "").strip()
        status = self.request.GET.get("status", "").strip()
        if q:
            qs = qs.filter(
                Q(username__icontains=q)
                | Q(first_name__icontains=q)
                | Q(last_name__icontains=q)
                | Q(email__icontains=q)
                | Q(phone__icontains=q)
            )
        if role:
            qs = qs.filter(role=role)
        if status == "active":
            qs = qs.filter(is_active=True)
        elif status == "inactive":
            qs = qs.filter(is_active=False)
        return qs

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx.update(
            {
                "roles": Role.choices,
                "q": self.request.GET.get("q", ""),
                "selected_role": self.request.GET.get("role", ""),
                "selected_status": self.request.GET.get("status", ""),
                "admin_count": User.objects.admins().filter(is_active=True).count(),
                "manager_count": User.objects.managers().filter(is_active=True).count(),
            }
        )
        return ctx


class UserDetailView(AdminRequiredMixin, DetailView):
    model = User
    template_name = "accounts/user_detail.html"
    context_object_name = "target_user"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["recent_activity"] = AuditLog.objects.filter(user=self.object)[:30]
        return ctx


class UserCreateView(AdminRequiredMixin, CreateView):
    model = User
    form_class = UserCreateForm
    template_name = "accounts/user_form.html"
    success_url = reverse_lazy("accounts:user_list")

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["title"] = "Add User"
        return ctx

    def form_valid(self, form):
        response = super().form_valid(form)
        log_action(
            AuditAction.CREATE,
            instance=self.object,
            description=f"Created {self.object.get_role_display()} account '{self.object.username}'.",
        )
        messages.success(self.request, f"User '{self.object.username}' created.")
        return response


class UserUpdateView(AdminRequiredMixin, UpdateView):
    model = User
    form_class = UserUpdateForm
    template_name = "accounts/user_form.html"
    success_url = reverse_lazy("accounts:user_list")

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["editing_user"] = self.request.user
        return kwargs

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["title"] = f"Edit {self.object.username}"
        return ctx

    def form_valid(self, form):
        before = User.objects.get(pk=self.object.pk)
        response = super().form_valid(form)
        changes = diff_instance(
            before,
            self.object,
            ["username", "first_name", "last_name", "email", "phone", "role", "is_active"],
        )
        log_action(
            AuditAction.UPDATE,
            instance=self.object,
            description=f"Updated account '{self.object.username}'.",
            changes=changes,
        )
        messages.success(self.request, "User updated.")
        return response


def user_toggle_active(request, pk):
    """Deactivate rather than delete - preserves every historical FK."""
    if not request.user.is_admin:
        messages.error(request, "Administrator privileges are required.")
        return redirect("core:forbidden")

    target = get_object_or_404(User, pk=pk)

    if target.pk == request.user.pk:
        messages.error(request, "You cannot deactivate your own account.")
        return redirect("accounts:user_list")

    if (
        target.is_active
        and target.role == Role.ADMIN
        and User.objects.admins().filter(is_active=True).exclude(pk=target.pk).count() == 0
    ):
        messages.error(request, "Cannot deactivate the only remaining administrator.")
        return redirect("accounts:user_list")

    if request.method == "POST":
        target.is_active = not target.is_active
        target.save(update_fields=["is_active"])
        state = "reactivated" if target.is_active else "deactivated"
        log_action(
            AuditAction.UPDATE,
            instance=target,
            description=f"Account '{target.username}' {state}.",
        )
        messages.success(request, f"Account '{target.username}' {state}.")
        return redirect("accounts:user_list")

    return render(request, "accounts/user_toggle_confirm.html", {"target_user": target})


def user_reset_password(request, pk):
    if not request.user.is_admin:
        messages.error(request, "Administrator privileges are required.")
        return redirect("core:forbidden")

    target = get_object_or_404(User, pk=pk)
    form = AdminPasswordResetForm(request.POST or None)

    if request.method == "POST" and form.is_valid():
        target.set_password(form.cleaned_data["new_password1"])
        target.must_change_password = form.cleaned_data["force_change"]
        target.save(update_fields=["password", "must_change_password"])
        log_action(
            AuditAction.OVERRIDE,
            instance=target,
            description=f"Administrator reset the password for '{target.username}'.",
        )
        messages.success(
            request,
            f"Password reset for '{target.username}'. Share it securely - it is not stored in readable form.",
        )
        return redirect("accounts:user_detail", pk=target.pk)

    return render(
        request,
        "accounts/user_password_reset.html",
        {"form": form, "target_user": target},
    )


# ---------------------------------------------------------------------------
# Self-service - any signed-in user
# ---------------------------------------------------------------------------
def profile(request):
    if not request.user.is_authenticated:
        return redirect("accounts:login")

    profile_form = SelfProfileForm(instance=request.user)
    password_form = PasswordChangeForm(user=request.user)

    if request.method == "POST":
        if "save_profile" in request.POST:
            # request.FILES is essential - without it the avatar silently
            # never arrives and the form "saves" with no photo.
            profile_form = SelfProfileForm(
                request.POST, request.FILES, instance=request.user
            )
            if profile_form.is_valid():
                profile_form.save()
                log_action(
                    AuditAction.UPDATE,
                    instance=request.user,
                    description="Updated own profile.",
                )
                messages.success(request, "Profile updated.")
                return redirect("accounts:profile")
        elif "change_password" in request.POST:
            password_form = PasswordChangeForm(user=request.user, data=request.POST)
            if password_form.is_valid():
                user = password_form.save()
                user.must_change_password = False
                user.save(update_fields=["must_change_password"])
                update_session_auth_hash(request, user)
                log_action(
                    AuditAction.UPDATE,
                    instance=user,
                    description="Changed own password.",
                )
                messages.success(request, "Password changed.")
                return redirect("accounts:profile")

    for form in (password_form,):
        for field in form.fields.values():
            field.widget.attrs.setdefault("class", "form-control")

    return render(
        request,
        "accounts/profile.html",
        {"profile_form": profile_form, "password_form": password_form},
    )


# ---------------------------------------------------------------------------
# Audit log - ADMIN ONLY
# ---------------------------------------------------------------------------
class AuditLogView(AdminRequiredMixin, ListView):
    model = AuditLog
    template_name = "accounts/audit_log.html"
    context_object_name = "entries"
    paginate_by = 50

    def get_queryset(self):
        qs = AuditLog.objects.select_related("user")
        action = self.request.GET.get("action", "").strip()
        user_id = self.request.GET.get("user", "").strip()
        model_name = self.request.GET.get("model", "").strip()
        q = self.request.GET.get("q", "").strip()
        if action:
            qs = qs.filter(action=action)
        if user_id:
            qs = qs.filter(user_id=user_id)
        if model_name:
            qs = qs.filter(model_name=model_name)
        if q:
            qs = qs.filter(
                Q(description__icontains=q)
                | Q(object_repr__icontains=q)
                | Q(username_snapshot__icontains=q)
            )
        return qs

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx.update(
            {
                "actions": AuditAction.choices,
                "all_users": User.objects.all().order_by("username"),
                "models": AuditLog.objects.exclude(model_name="")
                .values_list("model_name", flat=True)
                .distinct()
                .order_by("model_name"),
                "selected_action": self.request.GET.get("action", ""),
                "selected_user": self.request.GET.get("user", ""),
                "selected_model": self.request.GET.get("model", ""),
                "q": self.request.GET.get("q", ""),
            }
        )
        return ctx


class MyActivityView(StaffRequiredMixin, ListView):
    """A manager can review their own trail, but nobody else's."""

    model = AuditLog
    template_name = "accounts/my_activity.html"
    context_object_name = "entries"
    paginate_by = 40

    def get_queryset(self):
        return AuditLog.objects.filter(user=self.request.user)
