"""RBAC for the API, mirroring core.mixins on the web side."""
from rest_framework import permissions


class IsAdmin(permissions.BasePermission):
    message = "Administrator privileges are required."

    def has_permission(self, request, view):
        return bool(
            request.user
            and request.user.is_authenticated
            and request.user.is_active
            and request.user.is_admin
        )


class IsStaff(permissions.BasePermission):
    """Admin or Manager - i.e. any signed-in, active employee."""

    message = "You do not have permission to perform this action."

    def has_permission(self, request, view):
        return bool(
            request.user
            and request.user.is_authenticated
            and request.user.is_active
        )


class ReadOnlyOrAdmin(permissions.BasePermission):
    """
    Everyone may read; only an Admin may write.

    Used where a Manager needs the data to do their job but changing it is a
    financial or structural decision.
    """

    message = "Only an administrator may change this."

    def has_permission(self, request, view):
        if not (request.user and request.user.is_authenticated and request.user.is_active):
            return False
        if request.method in permissions.SAFE_METHODS:
            return True
        return request.user.is_admin


class StaffWriteAdminDelete(permissions.BasePermission):
    """
    Managers may create and edit. Only Admins may delete.

    Deletion is an override: it rewrites history, so it stays with the people
    accountable for the books.
    """

    message = "Only an administrator may delete records."

    def has_permission(self, request, view):
        if not (request.user and request.user.is_authenticated and request.user.is_active):
            return False
        if request.method == "DELETE":
            return request.user.is_admin
        return True
