"""
API access control, reading the same permission catalogue as the web side.

There is exactly one rule engine in this project: core/permissions.py declares
the codes, accounts.User.has_access answers the question, and both clients ask
it. The mobile app is a transport, not a second implementation - if the API
enforced its own idea of who may void a sale, one of the two clients would be
wrong and nobody would find out until the books stopped balancing.

    class ProductViewSet(viewsets.ModelViewSet):
        permission_classes = [HasPermission]
        permission_map = {
            "GET": "product.view",
            "POST": "product.create",
            "PATCH": "product.edit",
            "DELETE": "product.archive",
        }
"""
from rest_framework import permissions

SAFE = permissions.SAFE_METHODS


def _active(request) -> bool:
    user = getattr(request, "user", None)
    return bool(user and user.is_authenticated and user.is_active)


def _can(request, *codes, require_all=False) -> bool:
    checker = getattr(getattr(request, "user", None), "has_access", None)
    if checker is None:
        return False
    return checker(*codes, require_all=require_all)


class HasPermission(permissions.BasePermission):
    """
    Reads `permission_map` (per HTTP method) or `required_permission` off the
    view. A view that declares neither is refused rather than allowed: a
    missing requirement is far more likely to be an oversight than a decision
    to make the endpoint public.
    """

    message = "You do not have permission to perform this action."

    def has_permission(self, request, view):
        if not _active(request):
            return False

        code = None
        mapping = getattr(view, "permission_map", None)
        if mapping:
            code = mapping.get(request.method)
            if code is None and request.method in SAFE:
                code = mapping.get("GET")
        if code is None:
            code = getattr(view, "required_permission", None)

        if code is None:
            return False
        if code == "*":  # explicit "any signed-in user"
            return True
        codes = [code] if isinstance(code, str) else list(code)
        return _can(request, *codes)


class ActionPermission(HasPermission):
    """
    Adds per-action codes for DRF `@action` methods on a ViewSet.

        action_permissions = {"restock": "stock.restock"}

    Falls back to the method map for the standard list/retrieve/create verbs.
    """

    def has_permission(self, request, view):
        action = getattr(view, "action", None)
        per_action = getattr(view, "action_permissions", {}) or {}
        if action in per_action:
            code = per_action[action]
            if code == "*":
                return _active(request)
            codes = [code] if isinstance(code, str) else list(code)
            return _active(request) and _can(request, *codes)
        return super().has_permission(request, view)


class Permission(permissions.BasePermission):
    """
    One-off permission class for a function view.

        @permission_classes([Permission("report.profit")])

    Written as a callable class so it reads the same as DRF's built-ins at the
    call site while still carrying a code.
    """

    def __init__(self, *codes, require_all=True):
        self.codes = codes
        self.require_all = require_all
        self.message = "You do not have permission to perform this action."

    def __call__(self):
        # DRF instantiates whatever is in permission_classes; returning self
        # lets an already-constructed instance be used directly.
        return self

    def has_permission(self, request, view):
        return _active(request) and _can(
            request, *self.codes, require_all=self.require_all
        )


def requires(*codes, require_all=True):
    """Shorthand: `permission_classes=[requires('report.profit')]`."""
    return Permission(*codes, require_all=require_all)


# ---------------------------------------------------------------------------
# Legacy classes, kept so nothing breaks mid-migration
# ---------------------------------------------------------------------------
class IsAdmin(permissions.BasePermission):
    """Full control. Now defined by permission rather than by role string."""

    message = "Administrator privileges are required."

    def has_permission(self, request, view):
        return _active(request) and bool(request.user.is_admin)


class IsStaff(permissions.BasePermission):
    """Any signed-in, active employee."""

    message = "You do not have permission to perform this action."

    def has_permission(self, request, view):
        return _active(request)


class ReadOnlyOrAdmin(permissions.BasePermission):
    """Everyone may read; only full-control users may write."""

    message = "Only an administrator may change this."

    def has_permission(self, request, view):
        if not _active(request):
            return False
        if request.method in SAFE:
            return True
        return bool(request.user.is_admin)


class StaffWriteAdminDelete(permissions.BasePermission):
    """Staff may create and edit; deleting needs full control."""

    message = "Only an administrator may delete records."

    def has_permission(self, request, view):
        if not _active(request):
            return False
        if request.method == "DELETE":
            return bool(request.user.is_admin)
        return True
