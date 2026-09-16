"""REST API (только чтение, кроме отметки об исполнении поручения).

Все выборки ограничены комиссиями, доступными пользователю.
"""
from django.db.models import Q
from django.urls import NoReverseMatch, reverse
from django.utils import timezone
from drf_spectacular.utils import OpenApiParameter, extend_schema, extend_schema_view
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.response import Response

from apps.committees import access
from apps.committees.models import Commission, LegalAct, MemberRecord, Order
from apps.meetings.models import Meeting
from apps.protocols.models import Assignment, AssignmentComment, Protocol
from apps.voting.models import VotingSession

from . import serializers as s


def visible_ids(request):
    cache = getattr(request, "_api_visible_ids", None)
    if cache is None:
        cache = request._api_visible_ids = list(access.visible_commissions(request.user).values_list("pk", flat=True))
    return cache


def managed_ids(request):
    cache = getattr(request, "_api_managed_ids", None)
    if cache is None:
        cache = request._api_managed_ids = [
            c.pk for c in access.visible_commissions(request.user) if access.can_manage(request.user, c)
        ]
    return cache


def _int_param(request, name):
    value = request.query_params.get(name, "")
    return int(value) if value.isdigit() else None


COMMISSION_PARAM = OpenApiParameter("commission", int, description="ID комиссии")


class ScopedViewSet(viewsets.ReadOnlyModelViewSet):
    """Базовый ViewSet: при генерации схемы (без пользователя) отдаёт пустую выборку."""

    model = None

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return self.model.objects.none()
        return self.scoped_queryset()

    def scoped_queryset(self):
        raise NotImplementedError


@extend_schema(tags=["Комиссии"])
class CommissionViewSet(ScopedViewSet):
    model = Commission
    serializer_class = s.CommissionSerializer

    def scoped_queryset(self):
        return access.visible_commissions(self.request.user).prefetch_related("cities")

    @extend_schema(responses=s.MemberRecordSerializer(many=True), summary="Действующий состав")
    @action(detail=True, methods=["get"])
    def members(self, request, pk=None):
        commission = self.get_object()
        records = commission.members_on(timezone.localdate())
        return Response(s.MemberRecordSerializer(records, many=True).data)


@extend_schema(tags=["Комиссии"])
@extend_schema_view(list=extend_schema(parameters=[COMMISSION_PARAM], summary="История состава"))
class MemberRecordViewSet(ScopedViewSet):
    model = MemberRecord
    serializer_class = s.MemberRecordSerializer

    def scoped_queryset(self):
        qs = MemberRecord.objects.filter(commission_id__in=visible_ids(self.request)).select_related(
            "user", "start_order", "end_order"
        )
        commission = _int_param(self.request, "commission")
        if commission:
            qs = qs.filter(commission_id=commission)
        return qs.order_by("commission_id", "role_order", "user__last_name", "start_date")


@extend_schema(tags=["Комиссии"])
@extend_schema_view(list=extend_schema(parameters=[COMMISSION_PARAM]))
class OrderViewSet(ScopedViewSet):
    model = Order
    serializer_class = s.OrderSerializer

    def scoped_queryset(self):
        qs = Order.objects.filter(commission_id__in=visible_ids(self.request))
        commission = _int_param(self.request, "commission")
        if commission:
            qs = qs.filter(commission_id=commission)
        return qs.order_by("-signed_on", "-id")


@extend_schema(tags=["Комиссии"])
@extend_schema_view(list=extend_schema(parameters=[COMMISSION_PARAM]))
class LegalActViewSet(ScopedViewSet):
    model = LegalAct
    serializer_class = s.LegalActSerializer

    def scoped_queryset(self):
        qs = LegalAct.objects.filter(commission_id__in=visible_ids(self.request))
        commission = _int_param(self.request, "commission")
        if commission:
            qs = qs.filter(commission_id=commission)
        return qs.order_by("commission_id", "sort", "id")


@extend_schema(tags=["Заседания"])
@extend_schema_view(list=extend_schema(parameters=[COMMISSION_PARAM, OpenApiParameter("status", str)]))
class MeetingViewSet(ScopedViewSet):
    model = Meeting
    serializer_class = s.MeetingSerializer

    def scoped_queryset(self):
        qs = Meeting.objects.filter(commission_id__in=visible_ids(self.request)).prefetch_related("cities")
        commission = _int_param(self.request, "commission")
        if commission:
            qs = qs.filter(commission_id=commission)
        status_value = self.request.query_params.get("status")
        if status_value:
            qs = qs.filter(status=status_value)
        return qs.order_by("-starts_at")

    @extend_schema(responses=s.AgendaItemSerializer(many=True), summary="Повестка заседания")
    @action(detail=True, methods=["get"])
    def agenda(self, request, pk=None):
        meeting = self.get_object()
        items = meeting.agenda_items.select_related("speaker", "rubric").order_by("position", "id")
        return Response(s.AgendaItemSerializer(items, many=True).data)


@extend_schema(tags=["Голосования"])
@extend_schema_view(list=extend_schema(parameters=[COMMISSION_PARAM, OpenApiParameter("status", str)]))
class VotingSessionViewSet(ScopedViewSet):
    model = VotingSession
    serializer_class = s.VotingSessionSerializer

    def scoped_queryset(self):
        user = self.request.user
        qs = (
            VotingSession.objects.filter(Q(commission_id__in=visible_ids(self.request)) | Q(eligible=user))
            .distinct()
            .select_related("commission")
        )
        commission = _int_param(self.request, "commission")
        if commission:
            qs = qs.filter(commission_id=commission)
        status_value = self.request.query_params.get("status")
        if status_value:
            qs = qs.filter(status=status_value)
        return qs.order_by("-created_at")


@extend_schema(tags=["Протоколы"])
@extend_schema_view(list=extend_schema(parameters=[COMMISSION_PARAM, OpenApiParameter("status", str)]))
class ProtocolViewSet(ScopedViewSet):
    model = Protocol
    serializer_class = s.ProtocolSerializer

    def scoped_queryset(self):
        qs = (
            Protocol.objects.filter(commission_id__in=visible_ids(self.request))
            .filter(~Q(status=Protocol.Status.DRAFT) | Q(commission_id__in=managed_ids(self.request)))
            .prefetch_related("items", "decisions")
        )
        commission = _int_param(self.request, "commission")
        if commission:
            qs = qs.filter(commission_id=commission)
        status_value = self.request.query_params.get("status")
        if status_value:
            qs = qs.filter(status=status_value)
        return qs.order_by("-protocol_date", "-id")


@extend_schema(tags=["Поручения"])
@extend_schema_view(list=extend_schema(parameters=[
    COMMISSION_PARAM,
    OpenApiParameter("status", str, description="Статус поручения или overdue"),
    OpenApiParameter("responsible", str, description="ID ответственного или me"),
]))
class AssignmentViewSet(ScopedViewSet):
    model = Assignment
    serializer_class = s.AssignmentSerializer

    def scoped_queryset(self):
        user = self.request.user
        qs = (
            Assignment.objects.filter(Q(commission_id__in=visible_ids(self.request)) | Q(responsible=user))
            .exclude(decision__protocol__status=Protocol.Status.ANNULLED)
            .select_related("responsible", "decision")
            .distinct()
        )
        params = self.request.query_params
        commission = _int_param(self.request, "commission")
        if commission:
            qs = qs.filter(commission_id=commission)
        status_value = params.get("status")
        if status_value == "overdue":
            qs = qs.filter(status__in=Assignment.OPEN_STATUSES, due_date__lt=timezone.localdate())
        elif status_value:
            qs = qs.filter(status=status_value)
        responsible = params.get("responsible", "")
        if responsible == "me":
            qs = qs.filter(responsible=user)
        elif responsible.isdigit():
            qs = qs.filter(responsible_id=int(responsible))
        return qs.order_by("due_date", "id")

    @extend_schema(
        request=s.AssignmentReportSerializer, responses=s.AssignmentSerializer,
        summary="Отметить исполнение (ответственный)",
    )
    @action(detail=True, methods=["post"])
    def report(self, request, pk=None):
        assignment = self.get_object()
        if assignment.responsible_id != request.user.pk:
            raise PermissionDenied("Отметить исполнение может только ответственный исполнитель.")
        if assignment.status not in (Assignment.Status.ASSIGNED, Assignment.Status.IN_PROGRESS):
            raise ValidationError({"status": "Поручение не находится в работе."})
        payload = s.AssignmentReportSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        assignment.status = Assignment.Status.ON_CONFIRMATION
        assignment.reported_at = timezone.now()
        assignment.save(update_fields=["status", "reported_at"])
        AssignmentComment.objects.create(
            assignment=assignment, author=request.user, text=payload.validated_data.get("comment", ""),
            status_change=Assignment.Status.ON_CONFIRMATION,
        )
        self._notify(assignment)
        return Response(s.AssignmentSerializer(assignment, context={"request": request}).data, status=status.HTTP_200_OK)

    @staticmethod
    def _notify(assignment):
        from apps.notifications.models import Event
        from apps.notifications.services import notify

        secretary = assignment.commission.secretary
        if secretary is None:
            return
        try:
            url = reverse("protocols:assignment_detail", args=[assignment.pk])
        except NoReverseMatch:
            url = ""
        notify(
            secretary, Event.ASSIGNMENT, "Поручение отмечено исполненным — требуется подтверждение",
            f"Комиссия: {assignment.commission.name}.\nПоручение: {assignment.text[:300]}",
            url, commission=assignment.commission,
        )
