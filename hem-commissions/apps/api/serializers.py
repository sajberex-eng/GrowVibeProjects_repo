from django.urls import NoReverseMatch, reverse
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from apps.committees.models import Commission, LegalAct, MemberRecord, Order
from apps.meetings.models import AgendaItem, Meeting
from apps.protocols.models import Assignment, Decision, Protocol, ProtocolItem
from apps.voting.models import Vote, VotingSession


def _abs(request, name, *args):
    try:
        path = reverse(name, args=args)
    except NoReverseMatch:
        return None
    return request.build_absolute_uri(path) if request else path


class UserBriefSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    full_name = serializers.CharField()
    short_name = serializers.CharField()


class CommissionSerializer(serializers.ModelSerializer):
    cities = serializers.StringRelatedField(many=True)
    vote_quorum_label = serializers.CharField(read_only=True)
    meeting_quorum_label = serializers.CharField(read_only=True)

    class Meta:
        model = Commission
        fields = [
            "id", "name", "short_name", "kind", "description", "established_on", "cities", "is_active",
            "handles_patient_cases", "vote_days", "approval_days", "open_voting", "allow_vote_change",
            "vote_quorum_label", "meeting_quorum_label",
        ]


class MemberRecordSerializer(serializers.ModelSerializer):
    user = UserBriefSerializer()
    role_display = serializers.CharField(source="get_role_display")
    start_order = serializers.StringRelatedField()
    end_order = serializers.StringRelatedField()

    class Meta:
        model = MemberRecord
        fields = [
            "id", "commission", "user", "role", "role_display", "position_text", "city_text",
            "start_date", "end_date", "start_order", "end_order",
        ]


class OrderSerializer(serializers.ModelSerializer):
    class Meta:
        model = Order
        fields = ["id", "commission", "title", "number", "signed_on", "status", "note", "created_at"]


class LegalActSerializer(serializers.ModelSerializer):
    class Meta:
        model = LegalAct
        fields = [
            "id", "commission", "title", "number", "act_date", "authority", "url", "note", "status",
            "last_checked_at", "replaced_by",
        ]


class AgendaItemSerializer(serializers.ModelSerializer):
    speaker = serializers.CharField(source="speaker_display")
    rubric = serializers.StringRelatedField()

    class Meta:
        model = AgendaItem
        fields = [
            "id", "meeting", "position", "title", "description", "speaker", "duration_minutes", "rubric",
            "patient_id", "requires_vote",
        ]


class MeetingSerializer(serializers.ModelSerializer):
    cities = serializers.StringRelatedField(many=True)

    class Meta:
        model = Meeting
        fields = [
            "id", "commission", "kind", "starts_at", "duration_minutes", "format", "place", "video_link",
            "cities", "status", "original_starts_at", "cancel_reason", "agenda_status",
        ]


class VoteSerializer(serializers.ModelSerializer):
    user = UserBriefSerializer()
    entered_by_secretary = serializers.SerializerMethodField()

    class Meta:
        model = Vote
        fields = ["user", "choice", "comment", "cast_at", "entered_by_secretary"]

    def get_entered_by_secretary(self, obj) -> bool:
        return obj.entered_by_id is not None


class VotingSessionSerializer(serializers.ModelSerializer):
    results = serializers.SerializerMethodField()
    votes = serializers.SerializerMethodField()
    url = serializers.SerializerMethodField()

    class Meta:
        model = VotingSession
        fields = [
            "id", "uid", "commission", "meeting", "agenda_item", "question", "description", "deadline",
            "allow_comments", "status", "opened_at", "closed_at", "quorum_needed", "results", "votes", "url",
        ]

    def _visibility(self, obj):
        from apps.voting.services import results_visible_to

        cache = self.context.setdefault("_visibility", {})
        if obj.pk not in cache:
            cache[obj.pk] = results_visible_to(obj, self.context["request"].user)
        return cache[obj.pk]

    @extend_schema_field(OpenApiTypes.OBJECT)
    def get_results(self, obj):
        if not self._visibility(obj)["totals"] or obj.status != VotingSession.Status.CLOSED:
            return None
        return {
            "outcome": obj.outcome,
            "votes_for": obj.votes_for,
            "votes_against": obj.votes_against,
            "votes_abstain": obj.votes_abstain,
        }

    @extend_schema_field(VoteSerializer(many=True, allow_null=True))
    def get_votes(self, obj):
        if not self._visibility(obj)["per_person"]:
            return None
        votes = obj.votes.select_related("user").order_by("user__last_name")
        return VoteSerializer(votes, many=True).data

    def get_url(self, obj) -> str:
        return _abs(self.context.get("request"), "voting:detail", obj.pk)


class DecisionSerializer(serializers.ModelSerializer):
    class Meta:
        model = Decision
        fields = ["id", "item", "number", "text"]


class ProtocolItemSerializer(serializers.ModelSerializer):
    class Meta:
        model = ProtocolItem
        fields = ["id", "position", "title", "patient_id", "heard", "discussed", "resolved", "vote_summary", "voting", "agenda_item"]


class ProtocolSerializer(serializers.ModelSerializer):
    items = ProtocolItemSerializer(many=True, read_only=True)
    decisions = DecisionSerializer(many=True, read_only=True)
    links = serializers.SerializerMethodField()

    class Meta:
        model = Protocol
        fields = [
            "id", "uid", "commission", "kind", "meeting", "number", "protocol_date", "place", "format_text",
            "status", "revision", "signed_at", "signed_hash", "frozen_hash", "items", "decisions", "links",
        ]

    @extend_schema_field(OpenApiTypes.OBJECT)
    def get_links(self, obj):
        request = self.context.get("request")
        return {
            "detail": _abs(request, "protocols:detail", obj.pk),
            "signed_pdf": _abs(request, "protocols:download_signed", obj.pk) if obj.signed_pdf else None,
            "frozen_pdf": _abs(request, "protocols:download_frozen", obj.pk) if obj.frozen_pdf else None,
            "verify": obj.verify_url,
        }


class AssignmentSerializer(serializers.ModelSerializer):
    responsible = UserBriefSerializer(allow_null=True)
    responsible_display = serializers.CharField(read_only=True)
    is_overdue = serializers.BooleanField(read_only=True)
    protocol = serializers.IntegerField(source="decision.protocol_id", read_only=True)

    class Meta:
        model = Assignment
        fields = [
            "id", "commission", "decision", "protocol", "text", "responsible", "responsible_display", "due_date",
            "status", "is_overdue", "reported_at", "completed_on", "cancel_reason", "created_at",
        ]


class AssignmentReportSerializer(serializers.Serializer):
    comment = serializers.CharField(required=False, allow_blank=True, default="")
