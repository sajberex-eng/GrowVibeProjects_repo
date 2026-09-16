from django.contrib import admin

from .models import SignedReport


@admin.register(SignedReport)
class SignedReportAdmin(admin.ModelAdmin):
    list_display = ("__str__", "signed_by", "signed_at", "document_hash")
    list_filter = ("commission",)

    def get_readonly_fields(self, request, obj=None):
        return [f.name for f in self.model._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False
