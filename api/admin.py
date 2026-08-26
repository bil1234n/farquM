from django.contrib import admin

from .models import DeviceToken, NotificationLog


@admin.register(DeviceToken)
class DeviceTokenAdmin(admin.ModelAdmin):
    list_display = ("user", "platform", "device_name", "app_version",
                    "is_active", "last_seen")
    list_filter = ("platform", "is_active", "last_seen")
    search_fields = ("user__username", "device_name", "token")
    readonly_fields = ("token", "last_seen", "created_at", "updated_at")
    actions = ["send_test"]

    @admin.action(description="Send a test notification to these devices")
    def send_test(self, request, queryset):
        from .push import notify_users

        users = {d.user for d in queryset.select_related("user")}
        delivered = sum(
            notify_users(u, title="Test", body="Test notification from the admin site.",
                         channel="general")
            for u in users
        )
        self.message_user(request, f"Delivered to {delivered} device(s).")


@admin.register(NotificationLog)
class NotificationLogAdmin(admin.ModelAdmin):
    list_display = ("created_at", "user", "channel", "title",
                    "devices_targeted", "devices_delivered", "was_sent")
    list_filter = ("channel", "was_sent", "created_at")
    search_fields = ("title", "body", "user__username")
    date_hierarchy = "created_at"
    readonly_fields = [f.name for f in NotificationLog._meta.fields]

    def has_add_permission(self, request):
        return False
