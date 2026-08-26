"""
Role-Based Access Control (RBAC) building blocks.

Two roles exist:
    ADMIN   - full access: users, settings, financials, delete/override
    MANAGER - operational: products, sales, borrowers, inventory

Every view in this project must inherit from one of the mixins below,
or use the equivalent decorator. There is no "open" authenticated view.
"""
import functools

from django.contrib import messages
from django.contrib.auth.mixins import AccessMixin, LoginRequiredMixin
from django.core.exceptions import PermissionDenied
from django.http import HttpResponseRedirect
from django.shortcuts import redirect

from core.scoping import scoped, stamp_owner


class RoleRequiredMixin(LoginRequiredMixin, AccessMixin):
    """
    Restrict a class-based view to a set of roles.

    Usage:
        class ProfitReportView(RoleRequiredMixin, TemplateView):
            allowed_roles = ["ADMIN"]
    """

    allowed_roles: list[str] = []
    permission_denied_message = "You do not have permission to access this page."

    def dispatch(self, request, *args, **kwargs):
        if not request.user.is_authenticated:
            return self.handle_no_permission()

        if not request.user.is_active:
            messages.error(request, "Your account has been deactivated.")
            return redirect("accounts:logout")

        if self.allowed_roles and request.user.role not in self.allowed_roles:
            messages.error(request, self.permission_denied_message)
            return redirect("core:forbidden")

        return super().dispatch(request, *args, **kwargs)


class AdminRequiredMixin(RoleRequiredMixin):
    """Admin only: user management, settings, profit data, deletes/overrides."""

    allowed_roles = ["ADMIN"]
    permission_denied_message = (
        "Administrator privileges are required for this action."
    )


class StaffRequiredMixin(RoleRequiredMixin):
    """Admin or Manager: normal day-to-day operations."""

    allowed_roles = ["ADMIN", "MANAGER"]


class OwnerScopedMixin:
    """
    Restricts a ListView / DetailView / UpdateView to what the user may see.

    Mix this in ABOVE the Django generic view so its get_queryset() wraps the
    view's own. Because DetailView and UpdateView both fetch through
    get_queryset(), scoping the list also scopes the detail page - a Manager
    who types another manager's record ID into the URL gets a 404, not a 403.
    That is deliberate: a 403 would confirm the record exists.

    Set `scope_path` when the view's model reaches its owner by an unusual
    route; otherwise core.scoping works it out from the model.
    """

    scope_path: str | None = None

    def get_queryset(self):
        return scoped(super().get_queryset(), self.request.user, path=self.scope_path)


class AuthorStampMixin:
    """
    Automatically fills created_by / updated_by / owner on ModelForm views.
    Mix into CreateView / UpdateView.
    """

    def form_valid(self, form):
        obj = form.save(commit=False)
        if hasattr(obj, "created_by") and obj.pk is None:
            obj.created_by = self.request.user
        if hasattr(obj, "updated_by"):
            obj.updated_by = self.request.user
        # Ownership is stamped once, on creation, and never rewritten by an
        # edit. Reassigning a record between managers is an administrative
        # act, not a side effect of someone fixing a typo in a product name.
        stamp_owner(obj, self.request.user)
        obj.save()
        if hasattr(form, "save_m2m"):
            form.save_m2m()
        self.object = obj
        # Deliberately NOT calling ModelFormMixin.form_valid() here - it would
        # call form.save() a second time and issue a redundant UPDATE.
        return HttpResponseRedirect(self.get_success_url())


# ---------------------------------------------------------------------------
# Function-based-view decorators (same rules, different shape)
# ---------------------------------------------------------------------------
def role_required(*roles):
    """
    Decorator for function based views.

        @role_required("ADMIN")
        def delete_transaction(request, pk): ...
    """

    def decorator(view_func):
        @functools.wraps(view_func)
        def _wrapped(request, *args, **kwargs):
            if not request.user.is_authenticated:
                return redirect("accounts:login")
            if roles and request.user.role not in roles:
                raise PermissionDenied(
                    "You do not have permission to perform this action."
                )
            return view_func(request, *args, **kwargs)

        return _wrapped

    return decorator


admin_required = role_required("ADMIN")
staff_required = role_required("ADMIN", "MANAGER")


def get_owned_or_404(model_or_qs, user, **lookup):
    """
    Scoped replacement for get_object_or_404 in function-based views.

        txn = get_owned_or_404(Transaction, request.user, pk=pk)

    Raises Http404 for a record that exists but belongs to someone else, for
    the same reason OwnerScopedMixin does: "not found" leaks nothing, while
    "forbidden" confirms the ID is real.
    """
    from django.shortcuts import get_object_or_404

    qs = model_or_qs if hasattr(model_or_qs, "model") else model_or_qs._default_manager.all()
    return get_object_or_404(scoped(qs, user), **lookup)
