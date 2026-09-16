from django.contrib import admin

from .models import (
    AbsenceReason, Commission, CommissionAccess, DocumentTemplate, Holiday, LegalAct, LegalActCheck, MemberRecord,
    Order, Regulation, Rubric,
)


@admin.register(Holiday)
class HolidayAdmin(admin.ModelAdmin):
    list_display = ("date", "name", "is_working_day")
    list_filter = ("is_working_day",)
    date_hierarchy = "date"
    search_fields = ("name",)


@admin.register(AbsenceReason)
class AbsenceReasonAdmin(admin.ModelAdmin):
    list_display = ("name", "is_excused")
    list_filter = ("is_excused",)


@admin.register(DocumentTemplate)
class DocumentTemplateAdmin(admin.ModelAdmin):
    list_display = ("kind", "commission", "is_active", "uploaded_at")
    list_filter = ("kind", "is_active", "commission")
    fieldsets = ((None, {
        "fields": ("kind", "commission", "file", "is_active"),
        "description": (
            "DOCX-шаблон с плейсхолдерами Jinja2 (docxtpl). Для приказа о составе: {{ organization }}, "
            "{{ commission.name }}, {{ title }}, {{ number }}, {{ date }}, {{ preamble }}, списки add_list, "
            "remove_list, change_list и members (поля num, fio, role, position, city, basis). "
            "Образец: python manage.py write_order_template → docs/templates/order_template.docx"
        ),
    }),)


class RubricInline(admin.TabularInline):
    model = Rubric
    extra = 1


@admin.register(Commission)
class CommissionAdmin(admin.ModelAdmin):
    list_display = ("short_name", "name", "kind", "is_active", "handles_patient_cases", "established_on")
    list_filter = ("kind", "is_active", "handles_patient_cases", "cities")
    search_fields = ("name", "short_name")
    filter_horizontal = ("cities",)
    inlines = [RubricInline]
    fieldsets = (
        (None, {"fields": ("name", "short_name", "kind", "description", "established_on", "cities",
                           "is_active", "handles_patient_cases")}),
        ("Кворум", {"fields": ("meeting_quorum_numerator", "meeting_quorum_denominator", "meeting_quorum_strict",
                               "vote_quorum_numerator", "vote_quorum_denominator")}),
        ("Сроки и голосование", {"fields": ("approval_days", "vote_days", "open_voting", "allow_vote_change",
                                            "whatsapp_enabled")}),
    )


@admin.register(MemberRecord)
class MemberRecordAdmin(admin.ModelAdmin):
    list_display = ("commission", "user", "role", "position_text", "city_text", "start_date", "end_date",
                    "start_order", "end_order")
    list_filter = ("commission", "role")
    search_fields = ("user__last_name", "user__first_name", "position_text")
    list_select_related = ("commission", "user", "start_order", "end_order")
    date_hierarchy = "start_date"
    readonly_fields = ("commission", "user", "role", "start_date", "end_date", "start_order", "end_order")

    def has_add_permission(self, request):
        # Состав меняется только приказами (карточка комиссии → «Состав»).
        return False


@admin.register(Order)
class OrderAdmin(admin.ModelAdmin):
    list_display = ("__str__", "commission", "status", "number", "signed_on", "created_at")
    list_filter = ("status", "commission")
    search_fields = ("number", "title")
    readonly_fields = ("status", "created_by", "created_at")


@admin.register(Regulation)
class RegulationAdmin(admin.ModelAdmin):
    list_display = ("commission", "version", "title", "approved_on", "review_on", "expired_on")
    list_filter = ("commission",)
    readonly_fields = ("uploaded_by", "created_at")


@admin.register(LegalAct)
class LegalActAdmin(admin.ModelAdmin):
    list_display = ("title", "number", "commission", "status", "last_checked_at")
    list_filter = ("status", "commission")
    search_fields = ("title", "number", "note")
    readonly_fields = ("last_checked_at",)
    raw_id_fields = ("replaced_by",)


@admin.register(LegalActCheck)
class LegalActCheckAdmin(admin.ModelAdmin):
    list_display = ("checked_at", "act", "result", "manual", "initiated_by")
    list_filter = ("result", "manual")
    search_fields = ("act__title", "details")

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(CommissionAccess)
class CommissionAccessAdmin(admin.ModelAdmin):
    list_display = ("user", "commission", "granted_by", "granted_at")
    list_filter = ("commission",)
    raw_id_fields = ("user",)

    def save_model(self, request, obj, form, change):
        if not obj.granted_by_id:
            obj.granted_by = request.user
        super().save_model(request, obj, form, change)
