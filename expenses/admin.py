from django.contrib import admin

from .models import Employee, Expense


@admin.register(Employee)
class EmployeeAdmin(admin.ModelAdmin):
    list_display = ("name", "job_name", "phone", "monthly_salary", "is_active")
    list_filter = ("is_active",)
    search_fields = ("name", "phone")


@admin.register(Expense)
class ExpenseAdmin(admin.ModelAdmin):
    list_display = (
        "reference", "spent_on", "category_name", "amount", "payee",
        "employee", "owner", "is_voided",
    )
    list_filter = ("is_voided", "payment_method", "category_name")
    search_fields = ("reference", "payee", "notes")
    date_hierarchy = "spent_on"
    readonly_fields = ("reference",)
