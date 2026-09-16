from django.contrib import admin

from .models import JobRun, Notification, NotificationPreference


@admin.register(Notification)
class NotificationAdmin(admin.ModelAdmin):
    list_display = ("created_at", "user", "event", "title", "read_at", "emailed_at", "whatsapp_sent_at")
    list_filter = ("event", "commission")
    search_fields = ("title", "user__last_name", "user__username")
    date_hierarchy = "created_at"
    readonly_fields = ("created_at", "read_at", "emailed_at", "whatsapp_sent_at")


@admin.register(NotificationPreference)
class NotificationPreferenceAdmin(admin.ModelAdmin):
    list_display = ("user", "event", "email", "whatsapp")
    list_filter = ("event", "email", "whatsapp")
    search_fields = ("user__last_name", "user__username")


@admin.register(JobRun)
class JobRunAdmin(admin.ModelAdmin):
    list_display = ("name", "last_run")
    readonly_fields = ("name", "last_run", "last_result")

    def has_add_permission(self, request):
        return False
