"""Главная страница, глобальный поиск и страницы ошибок."""
from datetime import timedelta

from django.contrib.auth.decorators import login_required
from django.db.models import Q
from django.shortcuts import render
from django.utils import timezone

from . import access
from .models import LegalAct


def _visible_ids(user):
    return list(access.visible_commissions(user).values_list("pk", flat=True))


@login_required
def dashboard(request):
    from apps.meetings.models import Invitation, Meeting
    from apps.protocols.models import Assignment, Protocol, ProtocolApproval
    from apps.voting.models import VotingSession

    user = request.user
    now = timezone.now()
    today = timezone.localdate()
    commissions = access.visible_commissions(user).prefetch_related("cities")
    ids = [c.pk for c in commissions]
    invited_meeting_ids = list(
        Invitation.objects.filter(user=user, valid_until__gte=today).values_list("meeting_id", flat=True)
    )

    upcoming = (
        Meeting.objects.filter(Q(commission_id__in=ids) | Q(pk__in=invited_meeting_ids))
        .filter(starts_at__gte=now - timedelta(hours=3), status__in=[Meeting.Status.PLANNED, Meeting.Status.POSTPONED])
        .select_related("commission")
        .order_by("starts_at")[:8]
    )
    open_votes = (
        VotingSession.objects.filter(status=VotingSession.Status.OPEN, eligible=user, deadline__gt=now)
        .exclude(votes__user=user)
        .select_related("commission")
        .order_by("deadline")
    )
    pending_approvals = (
        ProtocolApproval.objects.filter(
            user=user, status=ProtocolApproval.Status.PENDING, protocol__status=Protocol.Status.APPROVAL,
        )
        .select_related("protocol", "protocol__commission")
        .order_by("protocol__approval_deadline")
    )
    pending_approvals = [a for a in pending_approvals if a.revision == a.protocol.revision]

    to_sign = []
    for protocol in Protocol.objects.filter(status=Protocol.Status.SIGNING, commission_id__in=ids).select_related("commission"):
        role = protocol.next_signer_role()
        if role and protocol.commission.officer(role, protocol.protocol_date) == user:
            to_sign.append(protocol)

    my_assignments = (
        Assignment.objects.filter(Q(responsible=user) | Q(co_executors=user), status__in=Assignment.OPEN_STATUSES)
        .select_related("commission")
        .distinct()
        .order_by("due_date")[:10]
    )

    managed = [c for c in commissions if access.can_manage(user, c)]
    managed_ids = [c.pk for c in managed]
    overdue_count = Assignment.objects.filter(
        commission_id__in=managed_ids, status__in=Assignment.OPEN_STATUSES, due_date__lt=today
    ).count()
    on_confirmation = Assignment.objects.filter(
        commission_id__in=managed_ids, status=Assignment.Status.ON_CONFIRMATION
    ).count()
    drafts = Protocol.objects.filter(commission_id__in=managed_ids, status=Protocol.Status.DRAFT).count()
    legal_alerts = (
        LegalAct.objects.filter(
            status__in=[LegalAct.Status.LOST_FORCE, LegalAct.Status.UNAVAILABLE, LegalAct.Status.NEEDS_CHECK]
        ).count()
        if user.is_admin else 0
    )

    return render(request, "home/dashboard.html", {
        "commissions": commissions,
        "upcoming": upcoming,
        "open_votes": open_votes,
        "pending_approvals": pending_approvals,
        "to_sign": to_sign,
        "my_assignments": my_assignments,
        "managed": managed,
        "overdue_count": overdue_count,
        "on_confirmation": on_confirmation,
        "drafts": drafts,
        "legal_alerts": legal_alerts,
        "today": today,
    })


@login_required
def search(request):
    from apps.protocols.models import Assignment, Decision, Protocol, ProtocolItem

    q = request.GET.get("q", "").strip()
    results = {}
    if len(q) >= 2:
        ids = _visible_ids(request.user)
        results["protocols"] = (
            ProtocolItem.objects.filter(protocol__commission_id__in=ids)
            .exclude(protocol__status=Protocol.Status.ANNULLED)
            .filter(
                Q(title__icontains=q) | Q(heard__icontains=q) | Q(discussed__icontains=q)
                | Q(resolved__icontains=q) | Q(patient_id__iexact=q) | Q(protocol__number__icontains=q)
            )
            .select_related("protocol", "protocol__commission")
            .order_by("-protocol__protocol_date")[:50]
        )
        results["decisions"] = (
            Decision.objects.filter(protocol__commission_id__in=ids, text__icontains=q)
            .select_related("protocol", "protocol__commission")
            .order_by("-protocol__protocol_date")[:50]
        )
        results["assignments"] = (
            Assignment.objects.filter(commission_id__in=ids)
            .filter(Q(text__icontains=q) | Q(responsible_name__icontains=q) | Q(responsible__last_name__icontains=q))
            .select_related("commission", "responsible")
            .order_by("-due_date")[:50]
        )
        results["legal_acts"] = (
            LegalAct.objects.filter(commission_id__in=ids)
            .filter(Q(title__icontains=q) | Q(number__icontains=q) | Q(note__icontains=q))
            .select_related("commission")[:50]
        )
    total = sum(len(v) for v in results.values())
    return render(request, "home/search.html", {"q": q, "results": results, "total": total})


def error_403(request, exception=None):
    message = str(exception) if exception and str(exception) else None
    template = "errors/403.html" if request.user.is_authenticated else "accounts/auth_base.html"
    return render(request, template, {"message": message}, status=403)


def error_404(request, exception=None):
    if not request.user.is_authenticated:
        return render(request, "accounts/auth_base.html", status=404)
    return render(request, "errors/404.html", status=404)
