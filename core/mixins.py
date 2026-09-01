"""
Access control building blocks for web views.

Every view in this project must declare what it needs. There is no "open"
authenticated view - a view with no requirement is a view nobody remembered to
think about.

    class ProfitReportView(PermissionRequiredMixin, TemplateView):
        required_permission = "report.profit"

    @permission_required("sale.void")
    def transaction_void(request, pk): ...

Permission codes come from core/permissions.py. The role mixins below
(AdminRequiredMixin, StaffRequiredMixin) predate the permission system and are
kept because a handful of views legitimately mean "anyone signed in"; they now
resolve through permissions rather than by comparing role strings.

TWO CHECKS, ALWAYS BOTH
-----------------------
A permission says WHAT you may do. OwnerScopedMixin / get_owned_or_404 say
WHICH ROWS. A view that checks one and not the other is a view where a sales
assistant can open a colleague's sale by guessing its ID.
"""
import functools

from django.contrib import messages
from django.contrib.auth.mixins import AccessMixin, LoginRequiredMixin
from django.core.exceptions import PermissionDenied
from django.http import HttpResponseRedirect
from django.shortcuts import redirect

from core.scoping import scoped, stamp_owner


def user_can(user, *codes, require_all=True) -> bool:
    """
    Permission check that tolerates anything being passed in.

    Templates, context processors and DRF all hand this an object that might
    be AnonymousUser, None, or a real User, and a permission helper that
    raises AttributeError on the anonymous case would turn "logged out" into a
    500 on the login page.
    """
    checker = getattr(user, "has_access", None)
    if checker is None:
        return False
    return checker(*codes, require_all=require_all)


class AccessRequiredMixin(LoginRequiredMixin, AccessMixin):
    """
    Base class: sign-in, active account, then the declared permissions.

    Subclasses set one of:
        required_permission   - a single code
        required_permissions  - a list; ALL are needed
        any_permission        - a list; ANY one is enough
    """

    required_permission: str | None = None
    required_permissions: list[str] = []
    any_permission: list[str] = []
    permission_denied_message = "You do not have permission to open this page."

    def get_required_permissions(self) -> list[str]:
        codes = list(self.required_permissions)
        if self.required_permission:
            codes.append(self.required_permission)
        return codes

    def has_required_access(self, user) -> bool:
        required = self.get_required_permissions()
        if required and not user_can(user, *required):
            return False
        if self.any_permission and not user_can(
            user, *self.any_permission, require_all=False
        ):
            return False
        return True

    def dispatch(self, request, *args, **kwargs):
        if not request.user.is_authenticated:
            return self.handle_no_permission()

        if not request.user.is_active:
            messages.error(request, "Your account has been deactivated.")
            return redirect("accounts:logout")

        if not self.has_required_access(request.user):
            messages.error(request, self.permission_denied_message)
            return redirect("core:forbidden")

        return super().dispatch(request, *args, **kwargs)


#: The name most views use.
PermissionRequiredMixin = AccessRequiredMixin


class RoleRequiredMixin(AccessRequiredMixin):
    """
    Kept for compatibility with views that were written against roles.

    `allowed_roles` is still honoured, but a permission requirement on the
    same view wins - so migrating a view means adding `required_permission`
    and deleting `allowed_roles`, in that order, without a window where the
    view is unguarded.
    """

    allowed_roles: list[str] = []

    def has_required_access(self, user):
        if not super().has_required_access(user):
            return False
        if self.allowed_roles and not self.get_required_permissions():
            return user.role in self.allowed_roles
        return True


class AdminRequiredMixin(AccessRequiredMixin):
    """Full-control screens: staff accounts, roles, settings, overrides."""

    required_permission = "user.permissions"
    permission_denied_message = (
        "Administrator privileges are required for this action."
    )


class StaffRequiredMixin(AccessRequiredMixin):
    """
    Any signed-in, active employee.

    Deliberately empty of permission requirements - it means "you are staff",
    not "you may see this". Views that show something specific declare a
    permission instead.
    """


class OwnerScopedMixin:
    """
    Restricts a ListView / DetailView / UpdateView to what the user may see.

    Mix this in ABOVE the Django generic view so its get_queryset() wraps the
    view's own. Because DetailView and UpdateView both fetch through
    get_queryset(), scoping the list also scopes the detail page - a user who
    types someone else's record ID into the URL gets a 404, not a 403. That is
    deliberate: a 403 would confirm the record exists.

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
        # edit. Reassigning a record between staff is an administrative act,
        # not a side effect of someone fixing a typo in a product name.
        stamp_owner(obj, self.request.user)
        obj.save()
        if hasattr(form, "save_m2m"):
            form.save_m2m()
        self.object = obj
        # Deliberately NOT calling ModelFormMixin.form_valid() here - it would
        # call form.save() a second time and issue a redundant UPDATE.
        return HttpResponseRedirect(self.get_success_url())


# ---------------------------------------------------------------------------
# Function-based-view helpers (same rules, different shape)
# ---------------------------------------------------------------------------
def permission_required(*codes, require_all=True, message=None):
    """
    Decorator for function based views.

        @permission_required("sale.void")
        def transaction_void(request, pk): ...
    """

    def decorator(view_func):
        @functools.wraps(view_func)
        def _wrapped(request, *args, **kwargs):
            if not request.user.is_authenticated:
                return redirect("accounts:login")
            if not request.user.is_active:
                messages.error(request, "Your account has been deactivated.")
                return redirect("accounts:logout")
            if not user_can(request.user, *codes, require_all=require_all):
                raise PermissionDenied(
                    message or "You do not have permission to perform this action."
                )
            return view_func(request, *args, **kwargs)

        return _wrapped

    return decorator


def role_required(*roles):
    """Legacy decorator. Prefer permission_required()."""

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


admin_required = permission_required("user.permissions")
staff_required = role_required("ADMIN", "MANAGER", "SALES")


def deny(request, message):
    """
    Standard refusal for a function-based view that checks inline.

        blocked = require(request, "sale.void", "Only ... may void a sale.")
        if blocked:
            return blocked
    """
    messages.error(request, message)
    return redirect("core:forbidden")


def require(request, *codes, message=None, require_all=True):
    """
    Returns a redirect response when the user lacks the permission, else None.

    Reads at the top of a view as a guard clause, and keeps the refusal
    message next to the rule it enforces.
    """
    if not request.user.is_authenticated:
        return redirect("accounts:login")
    if not user_can(request.user, *codes, require_all=require_all):
        return deny(
            request,
            message or "You do not have permission to perform this action.",
        )
    return None


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
