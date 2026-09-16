from django.contrib import admin

from .models import (
    AgendaAcknowledgement, AgendaItem, AgendaProposal, Attendance, Invitation, Material, Meeting, MeetingReschedule,
    Transcript,
)


class AgendaItemInline(admin.TabularInline):
    model = AgendaItem
    extra = 0
    fields = ("position", "title", "speaker", "speaker_name", "duration_minutes", "rubric", "patient_id", "requires_vote", "is_control_item")
    readonly_fields = ("is_control_item",)
    autocomplete_fields = ()
    show_change_link = True


class RescheduleInline(admin.TabularInline):
    model = MeetingReschedule
    extra = 0
    fields = ("old_starts_at", "new_starts_at", "reason", "changed_by", "changed_at")
    readonly_fields = fields
    can_delete = False


@admin.register(Meeting)
class MeetingAdmin(admin.ModelAdmin):
    list_display = ("commission", "starts_at", "kind", "format", "status", "agenda_status")
    list_filter = ("commission", "status", "kind", "format", "agenda_status")
    date_hierarchy = "starts_at"
    search_fields = ("place", "commission__short_name", "commission__name")
    readonly_fields = ("original_starts_at", "agenda_approved_at", "agenda_approved_by", "reminder_3d_sent",
                       "reminder_1d_sent", "created_by", "created_at")
    filter_horizontal = ("cities",)
    inlines = [AgendaItemInline, RescheduleInline]


@admin.register(Material)
class MaterialAdmin(admin.ModelAdmin):
    list_display = ("title", "meeting", "kind", "file_name", "url", "uploaded_by", "created_at")
    list_filter = ("kind", "meeting__commission")
    search_fields = ("title", "file_name")
    raw_id_fields = ("meeting", "agenda_item")


@admin.register(Attendance)
class AttendanceAdmin(admin.ModelAdmin):
    list_display = ("meeting", "user", "status", "reason", "is_invited")
    list_filter = ("status", "is_invited", "meeting__commission")
    raw_id_fields = ("meeting",)


@admin.register(Invitation)
class InvitationAdmin(admin.ModelAdmin):
    list_display = ("meeting", "user", "valid_until", "note", "created_by", "created_at")
    list_filter = ("meeting__commission",)
    raw_id_fields = ("meeting",)
    readonly_fields = ("created_by", "created_at")


@admin.register(AgendaProposal)
class AgendaProposalAdmin(admin.ModelAdmin):
    list_display = ("title", "meeting", "author", "status", "reviewed_by", "created_at")
    list_filter = ("status", "meeting__commission")
    search_fields = ("title", "description")
    raw_id_fields = ("meeting",)
    readonly_fields = ("created_at",)


@admin.register(AgendaAcknowledgement)
class AgendaAcknowledgementAdmin(admin.ModelAdmin):
    list_display = ("meeting", "user", "status", "created_at")
    list_filter = ("status",)
    raw_id_fields = ("meeting",)


@admin.register(Transcript)
class TranscriptAdmin(admin.ModelAdmin):
    list_display = ("meeting", "audio", "status", "created_by", "updated_at")
    list_filter = ("status",)
    raw_id_fields = ("meeting", "audio")
