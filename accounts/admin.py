"""
Django admin registrations.

This is the raw database editor, reachable only by a Django superuser. It
bypasses every business rule in the app - no stock movements, no audit
entries, no balance recalculation - so it exists as a recovery tool, not as
the place anybody works. Day-to-day access management lives in the app's own
Settings hub (core/views.py).
"""
from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as DjangoUserAdmin

from core.permissions import label_for

from .models import AuditLog, RoleDefinition, User


@admin.register(RoleDefinition)
class RoleDefinitionAdmin(admin.ModelAdmin):
    list_display = ("name", "code", "data_scope", "permission_summary", "is_system", "is_active")
    list_filter = ("is_system", "is_active", "data_scope")
    search_fields = ("code", "name", "description")
    ordering = ("rank", "name")

    @admin.display(description="Permissions")
    def permission_summary(self, obj):
        if obj.is_full_access:
            return "All (wildcard)"
        return f"{obj.permission_count} granted"

    def has_delete_permission(self, request, obj=None):
        # A built-in role cannot be deleted from here either: half the app
        # reads User.role and a missing role leaves those accounts with no
        # permissions at all.
        if obj is not None and obj.is_system:
            return False
        return super().has_delete_permission(request, obj)


@admin.register(User)
class UserAdmin(DjangoUserAdmin):
    list_display = (
        "username", "display_name", "role", "manager", "data_scope",
        "phone", "is_active", "last_login",
    )
    list_filter = ("role", "is_active", "is_staff", "data_scope_override")
    search_fields = ("username", "first_name", "last_name", "email", "phone")
    ordering = ("username",)
    readonly_fields = ("effective_permission_list",)

    fieldsets = DjangoUserAdmin.fieldsets + (
        (
            "Business role",
            {"fields": ("role", "manager", "phone", "employee_id",
                        "must_change_password", "notes")},
        ),
        (
            "Access overrides",
            {
                "fields": ("data_scope_override", "extra_permissions",
                           "denied_permissions", "effective_permission_list"),
                "description": (
                    "Edit these from Settings &rsaquo; Access Control instead - "
                    "that screen validates the codes, records who changed what, "
                    "and cannot lock the last administrator out of the system."
                ),
            },
        ),
    )
    add_fieldsets = DjangoUserAdmin.add_fieldsets + (
        ("Business role", {"fields": ("role", "manager", "phone", "employee_id")}),
    )

    @admin.display(description="Effective permissions")
    def effective_permission_list(self, obj):
        if not obj.pk:
            return "-"
        codes = sorted(obj.effective_permissions)
        if not codes:
            return "None"
        return ", ".join(label_for(c) for c in codes)


@admin.register(AuditLog)
class AuditLogAdmin(admin.ModelAdmin):
    list_display = ("created_at", "username_snapshot", "action", "model_name", "object_repr")
    list_filter = ("action", "model_name", "created_at")
    search_fields = ("description", "object_repr", "username_snapshot")
    date_hierarchy = "created_at"
    readonly_fields = [f.name for f in AuditLog._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
