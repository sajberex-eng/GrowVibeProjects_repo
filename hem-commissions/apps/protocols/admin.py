from django.contrib import admin

from .models import (
    Assignment,
    AssignmentComment,
    Decision,
    DissentingOpinion,
    Protocol,
    ProtocolApproval,
    ProtocolItem,
    Signature,
)


class ReadOnlyMixin:
    def has_add_permission(self, request, obj=None):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


class ProtocolItemInline(admin.StackedInline):
    model = ProtocolItem
    extra = 0
    fields = ["position", "title", "patient_id", "heard", "discussed", "resolved", "vote_summary", "flagged_names"]
    readonly_fields = ["flagged_names"]


class SignatureInline(ReadOnlyMixin, admin.TabularInline):
    model = Signature
    extra = 0
    fields = ["role", "full_name", "position_text", "signed_at", "ip_address", "document_hash", "revoked_at", "revoke_reason"]
    readonly_fields = fields


class ApprovalInline(ReadOnlyMixin, admin.TabularInline):
    model = ProtocolApproval
    extra = 0
    fields = ["revision", "user", "status", "item", "remarks", "responded_at"]
    readonly_fields = fields


@admin.register(Protocol)
class ProtocolAdmin(admin.ModelAdmin):
    list_display = ["__str__", "commission", "kind", "status", "revision", "protocol_date", "signed_at"]
    list_filter = ["commission", "kind", "status"]
    search_fields = ["number", "items__title", "items__patient_id"]
    date_hierarchy = "protocol_date"
    readonly_fields = [
        "uid", "status", "revision", "approval_started_at", "approval_deadline", "frozen_pdf", "frozen_hash",
        "signed_pdf", "signed_hash", "signed_at", "annulled_reason", "annulled_at", "replaces", "created_by",
        "created_at", "updated_at",
    ]
    exclude = ["votings"]
    inlines = [ProtocolItemInline, ApprovalInline, SignatureInline]

    def has_delete_permission(self, request, obj=None):
        return obj is None or obj.status == Protocol.Status.DRAFT


@admin.register(Signature)
class SignatureAdmin(ReadOnlyMixin, admin.ModelAdmin):
    list_display = ["protocol", "role", "full_name", "signed_at", "document_hash", "revoked_at"]
    list_filter = ["role"]
    search_fields = ["full_name", "document_hash", "protocol__number"]


@admin.register(ProtocolApproval)
class ProtocolApprovalAdmin(ReadOnlyMixin, admin.ModelAdmin):
    list_display = ["protocol", "revision", "user", "status", "responded_at"]
    list_filter = ["status"]


@admin.register(DissentingOpinion)
class DissentingOpinionAdmin(ReadOnlyMixin, admin.ModelAdmin):
    list_display = ["protocol", "item", "author", "signed_at", "withdrawn_at"]


class AssignmentCommentInline(ReadOnlyMixin, admin.TabularInline):
    model = AssignmentComment
    extra = 0
    fields = ["author", "text", "file_name", "status_change", "created_at"]
    readonly_fields = fields


@admin.register(Assignment)
class AssignmentAdmin(admin.ModelAdmin):
    list_display = ["__str__", "commission", "responsible", "responsible_name", "due_date", "status"]
    list_filter = ["commission", "status"]
    search_fields = ["text", "responsible_name", "responsible__last_name"]
    raw_id_fields = ["decision", "responsible", "confirmed_by"]
    filter_horizontal = ["co_executors"]
    inlines = [AssignmentCommentInline]


@admin.register(Decision)
class DecisionAdmin(admin.ModelAdmin):
    list_display = ["__str__", "protocol", "item"]
    search_fields = ["text", "number"]
    raw_id_fields = ["protocol", "item"]
