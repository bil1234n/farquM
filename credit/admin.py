from django.contrib import admin

from .models import CreditAccount, DebtRecord, Repayment, RepaymentProof


@admin.register(CreditAccount)
class CreditAccountAdmin(admin.ModelAdmin):
    list_display = ("customer", "credit_limit", "outstanding_balance",
                    "total_repaid", "is_blocked", "last_payment_date")
    list_filter = ("is_blocked",)
    search_fields = ("customer__name", "customer__phone")
    readonly_fields = ("total_credit_extended", "total_repaid", "outstanding_balance",
                       "last_purchase_date", "last_payment_date")
    actions = ["recalculate_selected"]

    @admin.action(description="Recalculate balances from the ledger")
    def recalculate_selected(self, request, queryset):
        for account in queryset:
            account.recalculate()
        self.message_user(request, f"Recalculated {queryset.count()} account(s).")


class RepaymentInline(admin.TabularInline):
    model = Repayment
    extra = 0
    readonly_fields = ("reference", "amount", "method", "paid_at", "balance_before",
                       "balance_after", "received_by", "is_reversed")
    can_delete = False


@admin.register(DebtRecord)
class DebtRecordAdmin(admin.ModelAdmin):
    list_display = ("reference", "customer", "principal", "amount_repaid",
                    "balance", "status", "due_date", "days_overdue")
    list_filter = ("status", "due_date", "issued_date")
    search_fields = ("reference", "customer__name", "customer__phone",
                     "transaction__reference")
    date_hierarchy = "issued_date"
    readonly_fields = ("reference", "amount_repaid", "balance", "created_at", "updated_at")
    inlines = [RepaymentInline]
    actions = ["recalculate_selected"]

    @admin.action(description="Recalculate from the repayment ledger")
    def recalculate_selected(self, request, queryset):
        for debt in queryset:
            debt.recalculate()
        self.message_user(request, f"Recalculated {queryset.count()} debt(s).")

    def has_delete_permission(self, request, obj=None):
        return False


class RepaymentProofInline(admin.TabularInline):
    model = RepaymentProof
    extra = 0


@admin.register(Repayment)
class RepaymentAdmin(admin.ModelAdmin):
    list_display = ("reference", "debt", "amount", "method", "paid_at",
                    "balance_before", "balance_after", "received_by", "is_reversed")
    list_filter = ("method", "is_reversed", "paid_at")
    search_fields = ("reference", "external_reference", "debt__reference",
                     "debt__customer__name")
    date_hierarchy = "paid_at"
    inlines = [RepaymentProofInline]
    readonly_fields = ("reference", "balance_before", "balance_after",
                       "reversed_at", "reversed_by")

    def has_delete_permission(self, request, obj=None):
        # Repayments are append-only. Reverse them instead.
        return False
