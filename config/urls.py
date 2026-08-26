from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.urls import include, path
from django.views.generic import RedirectView

urlpatterns = [
    path("django-admin/", admin.site.urls),
    path("", RedirectView.as_view(pattern_name="reports:dashboard", permanent=False)),
    path("system/", include("core.urls")),
    path("accounts/", include("accounts.urls")),
    path("inventory/", include("inventory.urls")),
    path("sales/", include("sales.urls")),
    path("credit/", include("credit.urls")),
    path("reports/", include("reports.urls")),
    path("api/", include("api.urls")),
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)

handler403 = "core.views.handler403"
handler404 = "core.views.handler404"
handler500 = "core.views.handler500"

admin.site.site_header = f"{settings.BUSINESS_NAME} Administration"
admin.site.site_title = settings.BUSINESS_NAME
admin.site.index_title = "System Administration"
