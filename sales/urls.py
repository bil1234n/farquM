from django.urls import path

from . import views

app_name = "sales"

urlpatterns = [
    # Customers
    path("customers/", views.CustomerListView.as_view(), name="customer_list"),
    path("customers/add/", views.CustomerCreateView.as_view(), name="customer_create"),
    path("customers/<int:pk>/", views.CustomerDetailView.as_view(), name="customer_detail"),
    path("customers/<int:pk>/edit/", views.CustomerUpdateView.as_view(), name="customer_update"),
    # Sales
    path("", views.TransactionListView.as_view(), name="transaction_list"),
    path("new/", views.sale_create, name="sale_create"),
    path("<int:pk>/", views.TransactionDetailView.as_view(), name="transaction_detail"),
    path("<int:pk>/void/", views.transaction_void, name="transaction_void"),
    path("<int:pk>/print/", views.transaction_print, name="transaction_print"),
    # Receipts
    path("<int:pk>/receipt/upload/", views.receipt_upload, name="receipt_upload"),
    path("receipts/<int:pk>/delete/", views.receipt_delete, name="receipt_delete"),
]
