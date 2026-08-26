from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as DjangoUserAdmin

from .models import AuditLog, User


@admin.register(User)
class UserAdmin(DjangoUserAdmin):
    list_display = ("username", "display_name", "role", "phone", "is_active", "last_login")
    list_filter = ("role", "is_active", "is_staff")
    search_fields = ("username", "first_name", "last_name", "email", "phone")
    ordering = ("username",)

    fieldsets = DjangoUserAdmin.fieldsets + (
        (
            "Business role",
            {"fields": ("role", "phone", "employee_id", "must_change_password", "notes")},
        ),
    )
    add_fieldsets = DjangoUserAdmin.add_fieldsets + (
        ("Business role", {"fields": ("role", "phone", "employee_id")}),
    )


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
