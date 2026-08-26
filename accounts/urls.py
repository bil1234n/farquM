from django.urls import path

from . import views

app_name = "accounts"

urlpatterns = [
    # Authentication
    path("login/", views.AppLoginView.as_view(), name="login"),
    path("logout/", views.AppLogoutView.as_view(), name="logout"),
    # Self service
    path("profile/", views.profile, name="profile"),
    path("my-activity/", views.MyActivityView.as_view(), name="my_activity"),
    # User management (Admin only)
    path("users/", views.UserListView.as_view(), name="user_list"),
    path("users/add/", views.UserCreateView.as_view(), name="user_create"),
    path("users/<int:pk>/", views.UserDetailView.as_view(), name="user_detail"),
    path("users/<int:pk>/edit/", views.UserUpdateView.as_view(), name="user_update"),
    path("users/<int:pk>/toggle/", views.user_toggle_active, name="user_toggle"),
    path(
        "users/<int:pk>/reset-password/",
        views.user_reset_password,
        name="user_reset_password",
    ),
    # Audit trail (Admin only)
    path("audit-log/", views.AuditLogView.as_view(), name="audit_log"),
]
