from django.urls import path

from . import views

app_name = "core"

urlpatterns = [
    path("forbidden/", views.forbidden, name="forbidden"),
    # Settings hub
    path("settings/", views.settings_hub, name="settings"),
    path("settings/business/", views.business_settings, name="business_settings"),
    path("settings/security/", views.security_settings, name="security_settings"),
    # Access control - who may do what
    path("access/", views.access_list, name="access_list"),
    path("access/<int:pk>/", views.user_access, name="user_access"),
    path("access/<int:pk>/reset/", views.user_access_reset, name="user_access_reset"),
    # Roles
    path("roles/", views.role_list, name="role_list"),
    path("roles/new/", views.role_form, name="role_create"),
    path("roles/<int:pk>/", views.role_form, name="role_update"),
    path("roles/<int:pk>/delete/", views.role_delete, name="role_delete"),
    path("roles/<int:pk>/reset/", views.role_reset, name="role_reset"),
]
