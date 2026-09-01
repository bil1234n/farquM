from django.urls import include, path
from rest_framework.routers import DefaultRouter

from . import views

app_name = "api"

router = DefaultRouter()
router.register("products", views.ProductViewSet, basename="product")
router.register("categories", views.CategoryViewSet, basename="category")
router.register("suppliers", views.SupplierViewSet, basename="supplier")
router.register("stock-movements", views.StockMovementViewSet, basename="stockmovement")
router.register("customers", views.CustomerViewSet, basename="customer")
router.register("sales", views.TransactionViewSet, basename="sale")
router.register("debts", views.DebtViewSet, basename="debt")
router.register("notifications", views.NotificationViewSet, basename="notification")
router.register("users", views.UserViewSet, basename="user")
router.register("roles", views.RoleViewSet, basename="role")

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

    # Aggregates
    path("dashboard/", views.dashboard, name="dashboard"),
    path("credit/overview/", views.credit_overview, name="credit_overview"),
    path("reports/profit/", views.profit_report, name="profit_report"),
    path("health/", views.health, name="health"),

    path("", include(router.urls)),
]
