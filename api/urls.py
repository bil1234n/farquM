from django.urls import include, path
from rest_framework.routers import DefaultRouter

from . import (
    delivery_views,
    expense_views,
    option_views,
    parity_views,
    production_views,
    request_views,
    views,
)

app_name = "api"

router = DefaultRouter()
router.register("products", views.ProductViewSet, basename="product")
router.register("categories", views.CategoryViewSet, basename="category")
router.register("suppliers", views.SupplierViewSet, basename="supplier")
router.register("stock-movements", views.StockMovementViewSet, basename="stockmovement")
router.register("materials", production_views.RawMaterialViewSet, basename="material")
router.register(
    "material-movements",
    production_views.MaterialMovementViewSet,
    basename="materialmovement",
)
router.register("recipes", production_views.RecipeViewSet, basename="recipe")
router.register(
    "production", production_views.ProductionRunViewSet, basename="productionrun"
)
router.register(
    "production-requests",
    request_views.ProductionRequestViewSet,
    basename="productionrequest",
)
# The managed pick-lists. Served to the phone AND to the web forms, which call
# it with the session cookie - one list, so a bank added on a phone is in the
# browser's dropdown a moment later.
router.register("options", option_views.OptionViewSet, basename="option")
router.register("customers", views.CustomerViewSet, basename="customer")
router.register("sales", views.TransactionViewSet, basename="sale")
router.register("debts", views.DebtViewSet, basename="debt")
router.register("notifications", views.NotificationViewSet, basename="notification")
router.register("users", views.UserViewSet, basename="user")
router.register("roles", views.RoleViewSet, basename="role")
# Goods leaving the yard, a sale at a time and a part at a time.
router.register("deliveries", delivery_views.DeliveryViewSet, basename="delivery")
# Money going out, and the people it goes to.
router.register("expenses", expense_views.ExpenseViewSet, basename="expense")
router.register("employees", expense_views.EmployeeViewSet, basename="employee")

urlpatterns = [
    # Auth
    path("auth/login/", views.LoginView.as_view(), name="login"),
    path("auth/logout/", views.LogoutView.as_view(), name="logout"),
    path("auth/me/", views.me, name="me"),
    path("auth/change-password/", views.change_password, name="change_password"),

    # Push registration
    path("devices/register/", views.RegisterDeviceView.as_view(), name="device_register"),

    # Access control & settings
    path("access/catalog/", views.permission_catalog, name="permission_catalog"),
    path("settings/", views.system_settings, name="system_settings"),

    # Registration passcodes - the phone's half of Settings -> Security.
    path(
        "settings/registration/",
        parity_views.registration_security,
        name="registration_security",
    ),

    # The yard. `plan` answers before anything is written, so a shortage is
    # on screen while the operator can still do something about it.
    path("production/plan/", production_views.production_plan,
         name="production_plan"),
    path("production/summary/", production_views.production_summary,
         name="production_summary"),

    # Aggregates
    path("dashboard/", views.dashboard, name="dashboard"),
    path("credit/overview/", views.credit_overview, name="credit_overview"),
    path("credit/borrowers/", parity_views.borrowers, name="borrowers"),

    # Reports. `index` is what the hub screen opens with: it lists only the
    # reports this person may actually open, so the app never offers a tile
    # that answers 403.
    path("reports/", parity_views.report_index, name="report_index"),
    path("reports/sales/", parity_views.sales_report, name="sales_report"),
    path("reports/inventory/", parity_views.inventory_report, name="inventory_report"),
    path(
        "reports/receivables/",
        parity_views.receivables_report,
        name="receivables_report",
    ),
    path("reports/profit/", views.profit_report, name="profit_report"),
    path("reports/export/sales/", parity_views.export_sales, name="export_sales"),
    path(
        "reports/export/receivables/",
        parity_views.export_receivables,
        name="export_receivables",
    ),

    # The permanent record.
    path("audit-log/", parity_views.audit_log, name="audit_log"),
    path("my-activity/", parity_views.my_activity, name="my_activity"),

    # Several pick-lists in one round trip, for a screen that needs the bank
    # list and the wallet list before the seller has chosen between them.
    path("options/bundle/", option_views.option_bundle, name="option_bundle"),

    path("health/", views.health, name="health"),

    path("", include(router.urls)),
]
