from django.contrib import admin

from .models import Category, Product, StockMovement, Supplier


@admin.register(Category)
class CategoryAdmin(admin.ModelAdmin):
    list_display = ("name", "product_count", "is_active", "created_at")
    list_filter = ("is_active",)
    search_fields = ("name",)
    prepopulated_fields = {"slug": ("name",)}


@admin.register(Supplier)
class SupplierAdmin(admin.ModelAdmin):
    list_display = ("name", "contact_person", "phone", "email", "is_active")
    list_filter = ("is_active",)
    search_fields = ("name", "phone", "contact_person", "email")


@admin.register(Product)
class ProductAdmin(admin.ModelAdmin):
    list_display = (
        "name", "sku", "category", "cost_price", "selling_price",
        "stock_quantity", "low_stock_threshold", "is_active", "is_deleted",
    )
    list_filter = ("is_active", "is_deleted", "category", "supplier", "unit")
    search_fields = ("name", "sku", "barcode")
    readonly_fields = ("stock_quantity", "created_at", "updated_at")
    autocomplete_fields = ("category", "supplier")
    fieldsets = (
        ("Identity", {"fields": ("name", "sku", "barcode", "category", "supplier", "unit", "description", "image")}),
        ("Pricing", {"fields": ("cost_price", "selling_price")}),
        ("Stock", {"fields": ("stock_quantity", "low_stock_threshold", "allow_negative_stock")}),
        ("Status", {"fields": ("is_active", "is_deleted", "deleted_at", "deleted_by")}),
        ("Audit", {"fields": ("created_by", "updated_by", "created_at", "updated_at"), "classes": ("collapse",)}),
    )

    def get_readonly_fields(self, request, obj=None):
        ro = list(self.readonly_fields)
        if obj:
            ro.append("sku")
        return ro


@admin.register(StockMovement)
class StockMovementAdmin(admin.ModelAdmin):
    list_display = (
        "created_at", "product", "movement_type", "quantity_delta",
        "quantity_before", "quantity_after", "reference", "performed_by",
    )
    list_filter = ("movement_type", "created_at")
    search_fields = ("product__name", "product__sku", "reference", "reason")
    date_hierarchy = "created_at"
    readonly_fields = [f.name for f in StockMovement._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
