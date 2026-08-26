from django.urls import path

from . import views

app_name = "reports"

urlpatterns = [
    path("", views.DashboardView.as_view(), name="dashboard"),
    path("sales/", views.SalesReportView.as_view(), name="sales_report"),
    path("profit/", views.ProfitReportView.as_view(), name="profit_report"),
    path("inventory/", views.InventoryReportView.as_view(), name="inventory_report"),
    path("receivables/", views.ReceivablesReportView.as_view(), name="receivables_report"),
    path("export/sales.csv", views.export_sales_csv, name="export_sales_csv"),
    path("export/receivables.csv", views.export_receivables_csv, name="export_receivables_csv"),
]
