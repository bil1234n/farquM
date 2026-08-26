from django.contrib import admin

from .models import Customer, Receipt, Transaction, TransactionItem


@admin.register(Customer)
class CustomerAdmin(admin.ModelAdmin):
    list_display = ("name", "phone", "customer_type", "is_credit_approved",
                    "outstanding_balance", "is_active")
    list_filter = ("customer_type", "is_credit_approved", "is_active")
    search_fields = ("name", "phone", "alternate_phone", "email")


class TransactionItemInline(admin.TabularInline):
    model = TransactionItem
    extra = 0
    readonly_fields = ("product_name", "product_sku", "unit_cost", "line_total")


class ReceiptInline(admin.TabularInline):
    model = Receipt
    extra = 0
    readonly_fields = ("uploaded_by", "created_at")


@admin.register(Transaction)
class TransactionAdmin(admin.ModelAdmin):
    list_display = ("reference", "created_at", "customer_display", "total_amount",
                    "amount_paid", "balance_due", "payment_status", "is_voided")
    list_filter = ("payment_status", "payment_method", "is_voided", "created_at")
    search_fields = ("reference", "customer__name", "customer__phone")
    date_hierarchy = "created_at"
    inlines = [TransactionItemInline, ReceiptInline]
    readonly_fields = ("reference", "subtotal", "total_amount", "balance_due",
                       "payment_status", "created_at", "updated_at",
                       "voided_at", "voided_by")

    def has_delete_permission(self, request, obj=None):
        return False  # Void instead - never destroy the audit trail.


@admin.register(Receipt)
class ReceiptAdmin(admin.ModelAdmin):
    list_display = ("transaction", "kind", "filename", "uploaded_by", "created_at")
    list_filter = ("kind", "created_at")
    search_fields = ("transaction__reference", "caption")
