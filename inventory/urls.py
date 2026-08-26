from django.urls import path

from . import views

app_name = "inventory"

urlpatterns = [
    # Products
    path("products/", views.ProductListView.as_view(), name="product_list"),
    path("products/add/", views.ProductCreateView.as_view(), name="product_create"),
    path("products/<int:pk>/", views.ProductDetailView.as_view(), name="product_detail"),
    path("products/<int:pk>/edit/", views.ProductUpdateView.as_view(), name="product_update"),
    path("products/<int:pk>/delete/", views.product_delete, name="product_delete"),
    # Stock
    path("products/<int:pk>/restock/", views.product_restock, name="product_restock"),
    path("products/<int:pk>/adjust/", views.product_adjust, name="product_adjust"),
    path("stock-movements/", views.StockMovementListView.as_view(), name="stock_movements"),
    path("low-stock/", views.LowStockView.as_view(), name="low_stock"),
    # Categories
    path("categories/", views.CategoryListView.as_view(), name="category_list"),
    path("categories/add/", views.CategoryCreateView.as_view(), name="category_create"),
    path("categories/<int:pk>/edit/", views.CategoryUpdateView.as_view(), name="category_update"),
    # Suppliers
    path("suppliers/", views.SupplierListView.as_view(), name="supplier_list"),
    path("suppliers/add/", views.SupplierCreateView.as_view(), name="supplier_create"),
    path("suppliers/<int:pk>/edit/", views.SupplierUpdateView.as_view(), name="supplier_update"),
    # API
    path("api/product-search/", views.product_search_api, name="product_search_api"),
]
