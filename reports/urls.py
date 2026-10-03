from django.urls import path

from . import views

app_name = "reports"

urlpatterns = [
    path("", views.DashboardView.as_view(), name="dashboard"),
    path("sales/", views.SalesReportView.as_view(), name="sales_report"),
    path("profit/", views.ProfitReportView.as_view(), name="profit_report"),
    path("audit/", views.AuditView.as_view(), name="audit"),
    path("audit/cost/<int:pk>/", views.audit_set_cost, name="audit_set_cost"),
    path("audit/correct/<slug:source>/<int:pk>/", views.audit_correct, name="audit_correct"),
    # One card opened up: money-out, money-in, profit, on-hand.
    path("audit/<slug:kind>/", views.AuditDetailView.as_view(), name="audit_detail"),
    path("inventory/", views.InventoryReportView.as_view(), name="inventory_report"),
    path("receivables/", views.ReceivablesReportView.as_view(), name="receivables_report"),
    path("export/sales.csv", views.export_sales_csv, name="export_sales_csv"),
    path("export/receivables.csv", views.export_receivables_csv, name="export_receivables_csv"),
]
