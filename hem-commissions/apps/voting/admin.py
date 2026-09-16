from django.contrib import admin

from .models import Vote, VotingMaterial, VotingSession


class VotingMaterialInline(admin.TabularInline):
    model = VotingMaterial
    extra = 0


class VoteInline(admin.TabularInline):
    model = Vote
    extra = 0
    readonly_fields = ("user", "choice", "comment", "cast_at", "entered_by")
    can_delete = False

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(VotingSession)
class VotingSessionAdmin(admin.ModelAdmin):
    list_display = ("question", "commission", "status", "outcome", "deadline", "created_at")
    list_filter = ("status", "outcome", "commission")
    search_fields = ("question",)
    readonly_fields = (
        "status", "opened_at", "closed_at", "outcome", "votes_for", "votes_against", "votes_abstain", "quorum_needed",
    )
    inlines = [VotingMaterialInline, VoteInline]
