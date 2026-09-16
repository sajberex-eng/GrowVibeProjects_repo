from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.paginator import Paginator
from django.db.models import Q
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import NoReverseMatch, reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from apps.audit import services as audit
from apps.committees import access
from apps.committees.models import Commission
from apps.committees.services import serve_file
from apps.meetings.models import AgendaItem

from . import services
from .forms import CommissionSelectForm, EnterVoteForm, MaterialForm, VoteForm, VotingSessionForm
from .models import Vote, VotingMaterial, VotingSession


def _errors(request, exc):
    for msg in getattr(exc, "messages", [str(exc)]):
        messages.error(request, msg)


def _get_session(request, pk, manage=False):
    session = get_object_or_404(
        VotingSession.objects.select_related("commission", "meeting", "agenda_item", "initiated_by"), pk=pk
    )
    if manage:
        access.require(access.can_manage(request.user, session.commission), "Управлять голосованием может секретарь или председатель комиссии")
    elif not services.can_view_session(request.user, session):
        raise PermissionDenied("Нет доступа к голосованию")
    return session


def _manageable_commissions(user):
    return Commission.objects.filter(
        pk__in=[c.pk for c in access.visible_commissions(user) if access.can_manage(user, c)]
    )


@login_required
def voting_list(request):
    user = request.user
    now = timezone.now()
    mine = (
        VotingSession.objects.filter(status=VotingSession.Status.OPEN, eligible=user, deadline__gt=now)
        .select_related("commission")
        .order_by("deadline")
    )
    voted_ids = set(Vote.objects.filter(user=user, session__in=mine).values_list("session_id", flat=True))
    my_rows = [{"session": s, "voted": s.pk in voted_ids} for s in mine]
    my_rows.sort(key=lambda r: r["voted"])

    commissions = access.visible_commissions(user)
    qs = (
        VotingSession.objects.filter(Q(commission__in=commissions) | Q(eligible=user))
        .distinct()
        .select_related("commission", "meeting")
        .order_by("-created_at")
    )
    commission_id = request.GET.get("commission", "")
    status = request.GET.get("status", "")
    kind = request.GET.get("kind", "")
    if commission_id.isdigit():
        qs = qs.filter(commission_id=int(commission_id))
    if status in VotingSession.Status.values:
        qs = qs.filter(status=status)
    if kind == "absentee":
        qs = qs.filter(meeting__isnull=True)
    elif kind == "meeting":
        qs = qs.filter(meeting__isnull=False)
    page = Paginator(qs, 30).get_page(request.GET.get("page"))
    managed = _manageable_commissions(user)
    return render(request, "voting/list.html", {
        "my_rows": my_rows,
        "page": page,
        "commissions": commissions,
        "statuses": VotingSession.Status.choices,
        "f_commission": commission_id,
        "f_status": status,
        "f_kind": kind,
        "managed": managed,
    })


@login_required
def voting_detail(request, pk):
    session = _get_session(request, pk)
    user = request.user
    commission = session.commission
    can_manage = access.can_manage(user, commission)
    is_eligible = session.eligible.filter(pk=user.pk).exists()
    my_vote = Vote.objects.filter(session=session, user=user).select_related("entered_by").first()
    visibility = services.results_visible_to(session, user)
    can_vote = session.is_open and is_eligible and (my_vote is None or commission.allow_vote_change)
    vote_form = None
    if can_vote:
        vote_form = VoteForm(initial={"choice": my_vote.choice, "comment": my_vote.comment} if my_vote else None)

    votes = list(session.votes.select_related("user", "entered_by").order_by("user__last_name"))
    eligible = list(session.eligible.all().order_by("last_name", "first_name"))
    voted_ids = {v.user_id for v in votes}
    not_voted = [u for u in eligible if u.pk not in voted_ids] if visibility["progress_names"] else []
    # поимённый список без выбора — кто проголосовал (виден всем, у кого есть доступ)
    voters = [v.user for v in votes]

    enter_form = None
    if session.is_open and access.is_secretary(user, commission):
        pending = session.eligible.exclude(pk=user.pk)
        if not commission.allow_vote_change:
            pending = pending.exclude(pk__in=list(voted_ids))
        if pending.exists():
            enter_form = EnterVoteForm(voters=pending.order_by("last_name"), prefix="enter")

    return render(request, "voting/detail.html", {
        "session": session,
        "commission": commission,
        "can_manage": can_manage,
        "is_eligible": is_eligible,
        "my_vote": my_vote,
        "can_vote": can_vote,
        "vote_form": vote_form,
        "visibility": visibility,
        "progress": services.progress(session),
        "votes": votes,
        "voters": voters,
        "not_voted": not_voted,
        "materials": session.materials.all(),
        "material_form": MaterialForm() if can_manage and session.status == VotingSession.Status.DRAFT else None,
        "enter_form": enter_form,
        "summary": services.outcome_summary(session),
    })


def _create(request, commission, initial, meeting=None, agenda_item=None):
    form = VotingSessionForm(request.POST or None, initial=initial)
    if request.method == "POST" and form.is_valid():
        session = form.save(commit=False)
        session.commission = commission
        session.meeting = meeting
        session.agenda_item = agenda_item
        session.initiated_by = request.user
        session.save()
        messages.success(request, "Голосование создано. Добавьте материалы и откройте голосование.")
        return redirect("voting:detail", session.pk)
    return form


@login_required
def voting_create(request):
    """Заочное голосование."""
    user = request.user
    managed = _manageable_commissions(user)
    if not managed.exists():
        raise PermissionDenied("Создавать голосования может секретарь или председатель комиссии")
    commission_id = request.POST.get("commission") or request.GET.get("commission")
    commission = None
    if commission_id and str(commission_id).isdigit():
        commission = access.get_commission_or_403(user, int(commission_id), manage=True)
    if commission is None and managed.count() == 1:
        commission = managed.first()
    select_form = CommissionSelectForm(
        initial={"commission": commission}, commissions=managed, prefix=None,
    )
    if commission is None:
        return render(request, "voting/form.html", {"select_form": select_form, "title": "Новое заочное голосование"})
    result = _create(request, commission, {"deadline": services.default_deadline(commission)})
    if not hasattr(result, "is_valid"):
        return result
    return render(request, "voting/form.html", {
        "form": result, "commission": commission, "select_form": select_form,
        "title": "Новое заочное голосование",
    })


@login_required
def voting_create_for_item(request, item_pk):
    item = get_object_or_404(AgendaItem.objects.select_related("meeting", "meeting__commission"), pk=item_pk)
    commission = item.meeting.commission
    access.require(access.can_manage(request.user, commission), "Создавать голосования может секретарь или председатель комиссии")
    initial = {
        "question": item.title,
        "description": item.description,
        "deadline": services.default_deadline(commission),
    }
    result = _create(request, commission, initial, meeting=item.meeting, agenda_item=item)
    if not hasattr(result, "is_valid"):
        return result
    return render(request, "voting/form.html", {
        "form": result, "commission": commission, "item": item,
        "title": "Голосование по вопросу повестки",
    })


@login_required
def voting_edit(request, pk):
    session = _get_session(request, pk, manage=True)
    if session.status != VotingSession.Status.DRAFT:
        messages.error(request, "Изменять можно только голосование в статусе «Подготовка».")
        return redirect("voting:detail", pk)
    form = VotingSessionForm(request.POST or None, instance=session)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, "Изменения сохранены.")
        return redirect("voting:detail", pk)
    return render(request, "voting/form.html", {
        "form": form, "session": session, "commission": session.commission, "title": "Изменение голосования",
    })


@login_required
@require_POST
def material_add(request, pk):
    session = _get_session(request, pk, manage=True)
    if session.status != VotingSession.Status.DRAFT:
        messages.error(request, "Материалы добавляются до открытия голосования.")
        return redirect("voting:detail", pk)
    form = MaterialForm(request.POST, request.FILES)
    if form.is_valid():
        form.save(session)
        messages.success(request, "Материал добавлен.")
    else:
        for errors in form.errors.values():
            for e in errors:
                messages.error(request, e)
    return redirect("voting:detail", pk)


@login_required
@require_POST
def material_delete(request, pk):
    material = get_object_or_404(VotingMaterial.objects.select_related("session"), pk=pk)
    session = _get_session(request, material.session_id, manage=True)
    if session.status != VotingSession.Status.DRAFT:
        messages.error(request, "Материалы открытого голосования не удаляются.")
    else:
        if material.file:
            material.file.delete(save=False)
        material.delete()
        messages.success(request, "Материал удалён.")
    return redirect("voting:detail", session.pk)


@login_required
def material_download(request, pk):
    material = get_object_or_404(VotingMaterial.objects.select_related("session"), pk=pk)
    _get_session(request, material.session_id)
    if material.url and not material.file:
        return redirect(material.url)
    if not material.file:
        raise PermissionDenied("Файл отсутствует")
    return serve_file(material.file, material.file_name or None)


@login_required
@require_POST
def voting_open(request, pk):
    session = _get_session(request, pk, manage=True)
    try:
        services.open_session(session, request.user)
        messages.success(request, "Голосование открыто. Члены комиссии получили уведомление со ссылкой.")
    except ValidationError as exc:
        _errors(request, exc)
    return redirect("voting:detail", pk)


@login_required
@require_POST
def voting_vote(request, pk):
    session = _get_session(request, pk)
    form = VoteForm(request.POST)
    if not form.is_valid():
        messages.error(request, "Выберите вариант ответа.")
        return redirect("voting:detail", pk)
    try:
        services.cast_vote(session, request.user, form.cleaned_data["choice"], form.cleaned_data["comment"])
        messages.success(request, "Ваш голос принят.")
    except ValidationError as exc:
        _errors(request, exc)
    return redirect("voting:detail", pk)


@login_required
@require_POST
def voting_enter(request, pk):
    session = _get_session(request, pk)
    access.require(access.is_secretary(request.user, session.commission), "Вносить голоса может только секретарь комиссии")
    form = EnterVoteForm(request.POST, voters=session.eligible.all(), prefix="enter")
    if not form.is_valid():
        messages.error(request, "Укажите члена комиссии и вариант ответа.")
        return redirect("voting:detail", pk)
    voter = form.cleaned_data["user"]
    try:
        services.cast_vote(
            session, voter, form.cleaned_data["choice"], form.cleaned_data["comment"], entered_by=request.user
        )
        messages.success(request, f"Голос {voter.short_name} внесён секретарём.")
    except ValidationError as exc:
        _errors(request, exc)
    return redirect("voting:detail", pk)


@login_required
@require_POST
def voting_close(request, pk):
    session = _get_session(request, pk, manage=True)
    if session.status != VotingSession.Status.OPEN:
        messages.error(request, "Голосование не идёт.")
    else:
        session = services.close_session(session, user=request.user)
        messages.success(request, f"Голосование завершено: {session.get_outcome_display()}.")
    return redirect("voting:detail", pk)


@login_required
@require_POST
def voting_cancel(request, pk):
    session = _get_session(request, pk, manage=True)
    try:
        services.cancel_session(session, request.user, request.POST.get("reason", "").strip())
        messages.success(request, "Голосование отменено.")
    except ValidationError as exc:
        _errors(request, exc)
    return redirect("voting:detail", pk)


def _in_protocol_ids(commission):
    try:
        from apps.protocols.models import Protocol
    except ImportError:  # pragma: no cover
        return set()
    return set(
        VotingSession.objects.filter(commission=commission, protocols__isnull=False)
        .exclude(protocols__status=Protocol.Status.ANNULLED)
        .values_list("pk", flat=True)
    )


@login_required
def absentee_protocol(request):
    """Формирование протокола заочного голосования по нескольким завершённым голосованиям."""
    user = request.user
    managed = _manageable_commissions(user)
    if not managed.exists():
        raise PermissionDenied("Формировать протоколы может секретарь или председатель комиссии")
    commission_id = request.POST.get("commission") or request.GET.get("commission")
    commission = None
    if commission_id and str(commission_id).isdigit():
        commission = access.get_commission_or_403(user, int(commission_id), manage=True)
    elif managed.count() == 1:
        commission = managed.first()
    sessions = []
    if commission is not None:
        used = _in_protocol_ids(commission)
        sessions = [
            s for s in VotingSession.objects.filter(
                commission=commission, meeting__isnull=True, status=VotingSession.Status.CLOSED
            ).order_by("closed_at")
            if s.pk not in used
        ]
    if request.method == "POST" and commission is not None:
        ids = {int(x) for x in request.POST.getlist("sessions") if x.isdigit()}
        chosen = [s for s in sessions if s.pk in ids]
        if not chosen:
            messages.error(request, "Отметьте хотя бы одно завершённое заочное голосование.")
        else:
            from apps.protocols.services import create_absentee_protocol

            try:
                protocol = create_absentee_protocol(chosen, user)
            except ValidationError as exc:
                _errors(request, exc)
            else:
                messages.success(request, "Проект протокола заочного голосования сформирован.")
                try:
                    return redirect(reverse("protocols:detail", args=[protocol.pk]))
                except NoReverseMatch:
                    return redirect("voting:list")
    return render(request, "voting/absentee_protocol.html", {
        "commission": commission,
        "managed": managed,
        "sessions": sessions,
    })
