"""Заседания: календарь, карточка заседания, повестка, материалы, участники, экспорт."""
import calendar as pycalendar
from collections import defaultdict
from datetime import datetime, timedelta

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.paginator import Paginator
from django.db.models import Prefetch, Q
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import NoReverseMatch, reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from apps.committees import access as commission_access
from apps.committees.models import AbsenceReason, Commission
from apps.committees.services import serve_file

from . import docgen, ical, services, transcription
from .access import can_view_meeting, get_meeting_or_403
from .forms import (
    AcknowledgementForm, AgendaItemForm, CancelForm, InvitationForm, MaterialFileForm, MaterialLinkForm, MeetingForm,
    ProposalForm, ProposalRejectForm, RescheduleForm, ScheduleForm, TranscriptForm,
)
from .models import (
    AgendaAcknowledgement, AgendaItem, AgendaProposal, Attendance, Invitation, Material, Meeting, Transcript,
)

MONTHS = [
    "Январь", "Февраль", "Март", "Апрель", "Май", "Июнь",
    "Июль", "Август", "Сентябрь", "Октябрь", "Ноябрь", "Декабрь",
]
DOW = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
TABS = [
    ("agenda", "Повестка"),
    ("materials", "Материалы"),
    ("participants", "Участники"),
    ("proposals", "Предложения"),
    ("records", "Записи и транскрипты"),
    ("protocols", "Протоколы"),
]
INVITED_TABS = {"agenda", "materials", "protocols"}


def url_exists(name, *args):
    try:
        reverse(name, args=args)
        return True
    except NoReverseMatch:
        return False


def _back(meeting, tab=None):
    return redirect(services.meeting_url(meeting, tab))


def _require_manage(user, meeting):
    commission_access.require(
        commission_access.can_manage(user, meeting.commission), "Действие доступно секретарю или председателю комиссии"
    )


def _require_commission_view(user, meeting):
    """Доступ не для приглашённых: состав, наблюдатели, администратор."""
    commission_access.require(commission_access.can_view(user, meeting.commission), "Нет доступа")


def _int(value, default=None):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


# ---------- календарь и список ----------

@login_required
def calendar_view(request):
    user = request.user
    today = timezone.localdate()
    commissions = commission_access.visible_commissions(user)
    commission = None
    commission_pk = _int(request.GET.get("commission"))
    if commission_pk:
        commission = commission_access.get_commission_or_403(user, commission_pk)
    view = "year" if request.GET.get("view") == "year" else "month"
    meetings = services.visible_meetings(user)
    if commission:
        meetings = meetings.filter(commission=commission)
    if request.GET.get("cancelled") != "1":
        show_cancelled = False
        meetings = meetings.exclude(status=Meeting.Status.CANCELLED)
    else:
        show_cancelled = True

    ctx = {
        "commissions": commissions, "commission": commission, "view": view, "today": today,
        "show_cancelled": show_cancelled,
        "manageable": [c for c in commissions if commission_access.can_manage(user, c)],
    }
    if view == "year":
        year = _int(request.GET.get("year"), today.year)
        if not 1900 < year < 3000:
            year = today.year
        by_month = defaultdict(list)
        for m in meetings.filter(starts_at__year=year).order_by("starts_at"):
            by_month[timezone.localtime(m.starts_at).month].append(m)
        ctx.update({
            "year": year, "prev_year": year - 1, "next_year": year + 1,
            "months": [
                {"num": i, "name": MONTHS[i - 1], "meetings": by_month.get(i, []),
                 "param": f"{year}-{i:02d}"}
                for i in range(1, 13)
            ],
            "total": sum(len(v) for v in by_month.values()),
        })
    else:
        try:
            first = datetime.strptime(request.GET.get("month", ""), "%Y-%m").date()
        except ValueError:
            first = today.replace(day=1)
        weeks = pycalendar.Calendar(firstweekday=0).monthdatescalendar(first.year, first.month)
        start, end = weeks[0][0], weeks[-1][-1]
        by_day = defaultdict(list)
        for m in meetings.filter(starts_at__date__gte=start, starts_at__date__lte=end).order_by("starts_at"):
            by_day[timezone.localtime(m.starts_at).date()].append(m)
        prev_month = (first - timedelta(days=1)).replace(day=1)
        next_month = (first + timedelta(days=32)).replace(day=1)
        ctx.update({
            "month_title": f"{MONTHS[first.month - 1]} {first.year}",
            "year": first.year,
            "prev_month": f"{prev_month:%Y-%m}", "next_month": f"{next_month:%Y-%m}",
            "this_month": f"{today:%Y-%m}",
            "dow": DOW,
            "days": [
                {"date": d, "other": d.month != first.month, "today": d == today, "meetings": by_day.get(d, [])}
                for week in weeks for d in week
            ],
            "month_list": [m for d in sorted(by_day) if d.month == first.month for m in by_day[d]],
        })
    return render(request, "meetings/calendar.html", ctx)


@login_required
def meeting_list(request):
    user = request.user
    commissions = commission_access.visible_commissions(user)
    qs = services.visible_meetings(user).prefetch_related("protocols")
    commission_pk = _int(request.GET.get("commission"))
    status = request.GET.get("status", "")
    period = request.GET.get("period", "upcoming")
    year = _int(request.GET.get("year"))
    if commission_pk:
        qs = qs.filter(commission_id=commission_pk)
    if status in Meeting.Status.values:
        qs = qs.filter(status=status)
    now = timezone.now()
    if year:
        qs = qs.filter(starts_at__year=year)
    if period == "upcoming":
        qs = qs.filter(starts_at__gte=now - timedelta(hours=6)).order_by("starts_at")
    elif period == "past":
        qs = qs.filter(starts_at__lt=now).order_by("-starts_at")
    else:
        qs = qs.order_by("-starts_at")
    page = Paginator(qs, 30).get_page(request.GET.get("page"))
    return render(request, "meetings/list.html", {
        "page": page, "commissions": commissions, "commission_pk": commission_pk, "status": status,
        "period": period, "year": year or "", "statuses": Meeting.Status.choices,
        "can_create": any(commission_access.can_manage(user, c) for c in commissions),
    })


# ---------- заседание ----------

@login_required
def meeting_create(request):
    user = request.user
    manageable = Commission.objects.filter(
        pk__in=[c.pk for c in commission_access.visible_commissions(user) if commission_access.can_manage(user, c)]
    )
    commission = None
    commission_pk = _int(request.GET.get("commission"))
    if commission_pk:
        commission = get_object_or_404(Commission, pk=commission_pk)
        commission_access.require(commission_access.can_manage(user, commission), "Нет прав на ведение комиссии")
    elif not manageable.exists():
        raise PermissionDenied("Нет комиссий, в которых вы можете назначать заседания")
    initial = {}
    if request.GET.get("date"):
        try:
            d = datetime.strptime(request.GET["date"], "%Y-%m-%d")
            initial["starts_at"] = timezone.make_aware(d.replace(hour=14))
        except ValueError:
            pass
    form = MeetingForm(request.POST or None, initial=initial, commissions=manageable, commission=commission)
    if request.method == "POST" and form.is_valid():
        meeting = form.save(commit=False)
        meeting.commission = commission or form.cleaned_data["commission"]
        commission_access.require(commission_access.can_manage(user, meeting.commission))
        services.create_meeting(meeting, user)
        form.save_m2m()
        messages.success(request, "Заседание назначено, участники уведомлены.")
        return redirect("meetings:detail", meeting.pk)
    return render(request, "meetings/form.html", {
        "form": form, "commission": commission, "title": "Новое заседание",
    })


@login_required
def meeting_edit(request, pk):
    meeting = get_meeting_or_403(request.user, pk, manage=True)
    form = MeetingForm(request.POST or None, instance=meeting, editing=True)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, "Изменения сохранены.")
        return redirect("meetings:detail", meeting.pk)
    return render(request, "meetings/form.html", {
        "form": form, "meeting": meeting, "commission": meeting.commission, "title": "Редактирование заседания",
    })


@login_required
def meeting_reschedule(request, pk):
    meeting = get_meeting_or_403(request.user, pk, manage=True)
    if not meeting.is_upcoming:
        messages.error(request, "Перенести можно только запланированное заседание.")
        return _back(meeting)
    form = RescheduleForm(request.POST or None, initial={"new_starts_at": timezone.localtime(meeting.starts_at)})
    if request.method == "POST" and form.is_valid():
        try:
            services.reschedule_meeting(meeting, form.cleaned_data["new_starts_at"], form.cleaned_data["reason"], request.user)
        except ValidationError as exc:
            form.add_error(None, exc)
        else:
            messages.success(request, "Заседание перенесено, участники уведомлены.")
            return _back(meeting)
    return render(request, "meetings/simple_form.html", {
        "form": form, "meeting": meeting, "title": "Перенос заседания",
        "submit": "Перенести", "note": "Участники получат уведомление. Исходная дата сохраняется в карточке заседания.",
    })


@login_required
def meeting_cancel(request, pk):
    meeting = get_meeting_or_403(request.user, pk, manage=True)
    if not meeting.is_upcoming:
        messages.error(request, "Отменить можно только запланированное заседание.")
        return _back(meeting)
    form = CancelForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        services.cancel_meeting(meeting, form.cleaned_data["reason"], request.user)
        messages.success(request, "Заседание отменено, участники уведомлены.")
        return _back(meeting)
    return render(request, "meetings/simple_form.html", {
        "form": form, "meeting": meeting, "title": "Отмена заседания", "submit": "Отменить заседание", "danger": True,
    })


@login_required
@require_POST
def meeting_mark_held(request, pk):
    meeting = get_meeting_or_403(request.user, pk, manage=True)
    try:
        services.mark_held(meeting, request.user)
        messages.success(request, "Заседание отмечено как проведённое.")
    except ValidationError as exc:
        messages.error(request, "; ".join(exc.messages))
    return _back(meeting)


def _agenda_items(meeting):
    from apps.voting.models import VotingSession

    return list(
        meeting.agenda_items.select_related("speaker", "rubric", "proposal")
        .prefetch_related(
            Prefetch("votings", queryset=VotingSession.objects.order_by("-created_at")),
            Prefetch("materials", queryset=Material.objects.order_by("created_at")),
        )
        .order_by("position", "id")
    )


@login_required
def meeting_detail(request, pk):
    user = request.user
    meeting = get_meeting_or_403(user, pk)
    commission = meeting.commission
    member_view = commission_access.can_view(user, commission)
    invited_only = not member_view
    can_manage = commission_access.can_manage(user, commission)
    tabs = [(k, v) for k, v in TABS if member_view or k in INVITED_TABS]
    tab = request.GET.get("tab", "agenda")
    if tab not in dict(tabs):
        tab = "agenda"

    ctx = {
        "meeting": meeting, "commission": commission, "tabs": tabs, "tab": tab,
        "invited_only": invited_only, "can_manage": can_manage,
        "can_approve": services.can_approve_agenda(user, meeting),
        "can_propose": services.can_propose(user, meeting),
        "can_acknowledge": services.can_acknowledge(user, meeting),
        "my_ack": meeting.acknowledgements.filter(user=user).first(),
        "quorum": meeting.quorum(),
        "show_patient": commission.handles_patient_cases,
        "has_vote_create_url": url_exists("voting:create_for_item", 1),
        "has_vote_detail_url": url_exists("voting:detail", 1),
        "has_protocol_create_url": url_exists("protocols:create_from_meeting", meeting.pk),
        "has_protocol_detail_url": url_exists("protocols:detail", 1),
        "reschedules": meeting.reschedules.select_related("changed_by"),
        "new_proposals": meeting.proposals.filter(status=AgendaProposal.Status.NEW).count() if can_manage else 0,
        "invitation": meeting.invitations.filter(user=user).first() if invited_only else None,
    }
    items = _agenda_items(meeting)
    ctx["items"] = items
    ctx["total_minutes"] = sum(i.duration_minutes for i in items)
    if tab == "agenda":
        ctx["ack_form"] = AcknowledgementForm(initial={
            "status": ctx["my_ack"].status if ctx["my_ack"] else "ack",
            "text": ctx["my_ack"].text if ctx["my_ack"] else "",
        })
    elif tab == "materials":
        ctx["materials"] = meeting.materials.exclude(kind=Material.Kind.AUDIO).select_related("agenda_item", "uploaded_by")
        ctx["audio"] = meeting.materials.filter(kind=Material.Kind.AUDIO)
    elif tab == "participants":
        ctx["attendance"] = services.attendance_rows(meeting)
        ctx["invitations"] = meeting.invitations.select_related("user", "user__position").order_by("user__last_name")
        ctx["acks"] = services.ack_table(meeting) if meeting.agenda_status == Meeting.AgendaStatus.APPROVED else []
        ctx["ack_count"] = sum(1 for a in ctx["acks"] if a["ack"])
    elif tab == "proposals":
        qs = meeting.proposals.select_related("author", "reviewed_by")
        if not can_manage:
            qs = qs.filter(author=user)
        ctx["proposals"] = qs
    elif tab == "records":
        ctx["audio"] = meeting.materials.filter(kind__in=[Material.Kind.AUDIO, Material.Kind.VIDEO]).prefetch_related("transcript_set")
        ctx["transcripts"] = meeting.transcripts.select_related("audio", "created_by")
        ctx["transcription_configured"] = transcription.is_configured()
    elif tab == "protocols":
        ctx["protocols"] = meeting.protocols.all()
    return render(request, "meetings/detail.html", ctx)


# ---------- повестка ----------

def _agenda_edit_guard(request, meeting):
    _require_manage(request.user, meeting)
    if not meeting.is_upcoming and meeting.status != Meeting.Status.HELD:
        raise PermissionDenied("Заседание отменено — повестка не редактируется")


def _after_agenda_change(request, meeting):
    if services.agenda_changed(meeting):
        messages.warning(request, "Повестка изменена — требуется повторное утверждение председателем.")


@login_required
def item_add(request, pk):
    meeting = get_meeting_or_403(request.user, pk, manage=True)
    _agenda_edit_guard(request, meeting)
    form = AgendaItemForm(request.POST or None, commission=meeting.commission)
    if request.method == "POST" and form.is_valid():
        item = form.save(commit=False)
        item.meeting = meeting
        item.position = services.next_position(meeting)
        item.save()
        services.renumber(meeting)
        _after_agenda_change(request, meeting)
        messages.success(request, "Вопрос добавлен в повестку.")
        if "add_another" in request.POST:
            return redirect("meetings:item_add", meeting.pk)
        return _back(meeting)
    return render(request, "meetings/item_form.html", {"form": form, "meeting": meeting, "title": "Новый вопрос повестки"})


@login_required
def item_edit(request, item_pk):
    item = get_object_or_404(AgendaItem.objects.select_related("meeting__commission"), pk=item_pk)
    meeting = item.meeting
    _agenda_edit_guard(request, meeting)
    form = AgendaItemForm(request.POST or None, instance=item, commission=meeting.commission)
    if request.method == "POST" and form.is_valid():
        if form.has_changed():
            form.save()
            _after_agenda_change(request, meeting)
        messages.success(request, "Вопрос сохранён.")
        return _back(meeting)
    return render(request, "meetings/item_form.html", {"form": form, "meeting": meeting, "item": item, "title": "Вопрос повестки"})


@login_required
@require_POST
def item_delete(request, item_pk):
    item = get_object_or_404(AgendaItem.objects.select_related("meeting__commission"), pk=item_pk)
    meeting = item.meeting
    _agenda_edit_guard(request, meeting)
    if item.votings.exclude(status="draft").exists():
        messages.error(request, "По вопросу уже проводилось голосование — удаление невозможно.")
        return _back(meeting)
    item.delete()
    services.renumber(meeting)
    _after_agenda_change(request, meeting)
    messages.success(request, "Вопрос удалён из повестки.")
    return _back(meeting)


@login_required
@require_POST
def item_move(request, item_pk, direction):
    item = get_object_or_404(AgendaItem.objects.select_related("meeting__commission"), pk=item_pk)
    meeting = item.meeting
    _agenda_edit_guard(request, meeting)
    if direction not in ("up", "down"):
        raise Http404
    if services.move_item(item, direction):
        _after_agenda_change(request, meeting)
    return _back(meeting)


@login_required
@require_POST
def control_refresh(request, pk):
    meeting = get_meeting_or_403(request.user, pk, manage=True)
    item = services.ensure_control_item(meeting, force=True)
    if item is None:
        messages.info(request, "Открытых поручений нет — пункт «Контроль исполнения» не требуется.")
    else:
        messages.success(request, "Пункт «Контроль исполнения решений» обновлён.")
    return _back(meeting)


@login_required
@require_POST
def agenda_submit(request, pk):
    meeting = get_meeting_or_403(request.user, pk, manage=True)
    try:
        services.submit_agenda(meeting, request.user)
        messages.success(request, "Повестка направлена председателю на утверждение.")
    except ValidationError as exc:
        messages.error(request, "; ".join(exc.messages))
    return _back(meeting)


@login_required
@require_POST
def agenda_approve(request, pk):
    meeting = get_meeting_or_403(request.user, pk)
    commission_access.require(services.can_approve_agenda(request.user, meeting), "Утверждает повестку председатель комиссии")
    try:
        services.approve_agenda(meeting, request.user)
        messages.success(request, "Повестка утверждена. Состав и приглашённые уведомлены.")
    except ValidationError as exc:
        messages.error(request, "; ".join(exc.messages))
    return _back(meeting)


@login_required
@require_POST
def agenda_return(request, pk):
    meeting = get_meeting_or_403(request.user, pk)
    commission_access.require(services.can_approve_agenda(request.user, meeting), "Действие доступно председателю")
    services.return_agenda(meeting, request.user, request.POST.get("comment", "")[:500])
    messages.success(request, "Повестка возвращена секретарю на доработку.")
    return _back(meeting)


@login_required
@require_POST
def acknowledge(request, pk):
    meeting = get_meeting_or_403(request.user, pk)
    form = AcknowledgementForm(request.POST)
    if not form.is_valid():
        for errors in form.errors.values():
            messages.error(request, " ".join(errors))
        return _back(meeting)
    services.acknowledge(meeting, request.user, form.cleaned_data["status"], form.cleaned_data.get("text", ""))
    if form.cleaned_data["status"] == AgendaAcknowledgement.Status.ACK:
        messages.success(request, "Отмечено: ознакомлен с повесткой.")
    else:
        messages.success(request, "Предложение по повестке направлено секретарю.")
    return _back(meeting)


# ---------- предложения ----------

@login_required
def proposal_create(request, pk):
    meeting = get_meeting_or_403(request.user, pk)
    commission_access.require(
        services.can_propose(request.user, meeting),
        "Предлагать вопросы могут члены комиссии, пока открыт приём предложений",
    )
    form = ProposalForm(request.POST or None, request.FILES or None, commission=meeting.commission,
                        instance=AgendaProposal(meeting=meeting))
    if request.method == "POST" and form.is_valid():
        proposal = form.save(commit=False)
        services.create_proposal(proposal, request.user)
        messages.success(request, "Предложение направлено секретарю.")
        return _back(meeting, "proposals")
    return render(request, "meetings/proposal_form.html", {"form": form, "meeting": meeting})


def _proposal_for_manage(request, proposal_pk):
    proposal = get_object_or_404(AgendaProposal.objects.select_related("meeting__commission", "author"), pk=proposal_pk)
    _require_manage(request.user, proposal.meeting)
    return proposal


@login_required
@require_POST
def proposal_accept(request, proposal_pk):
    proposal = _proposal_for_manage(request, proposal_pk)
    try:
        services.accept_proposal(proposal, request.user)
        messages.success(request, "Вопрос включён в повестку, автор уведомлён.")
    except ValidationError as exc:
        messages.error(request, "; ".join(exc.messages))
    return _back(proposal.meeting, "proposals")


@login_required
def proposal_reject(request, proposal_pk):
    proposal = _proposal_for_manage(request, proposal_pk)
    form = ProposalRejectForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        try:
            services.reject_proposal(proposal, request.user, form.cleaned_data["response"])
            messages.success(request, "Предложение отклонено, автор уведомлён.")
        except ValidationError as exc:
            messages.error(request, "; ".join(exc.messages))
        return _back(proposal.meeting, "proposals")
    return render(request, "meetings/simple_form.html", {
        "form": form, "meeting": proposal.meeting, "title": f"Отклонить предложение «{proposal.title}»",
        "submit": "Отклонить", "danger": True, "back_tab": "proposals",
    })


@login_required
def proposal_file(request, proposal_pk):
    proposal = get_object_or_404(AgendaProposal.objects.select_related("meeting__commission"), pk=proposal_pk)
    allowed = proposal.author_id == request.user.pk or commission_access.can_manage(request.user, proposal.meeting.commission)
    commission_access.require(allowed, "Нет доступа к файлу")
    if not proposal.file:
        raise Http404
    return serve_file(proposal.file, proposal.file_name or None)


# ---------- материалы ----------

@login_required
def material_add(request, pk):
    meeting = get_meeting_or_403(request.user, pk, manage=True)
    kind = request.GET.get("type", "file")
    initial = {}
    if request.GET.get("item"):
        initial["agenda_item"] = _int(request.GET["item"])
    if kind == "file":
        form = MaterialFileForm(request.POST or None, request.FILES or None, meeting=meeting, initial=initial,
                                instance=Material(meeting=meeting))
    else:
        if kind in (Material.Kind.AUDIO, Material.Kind.VIDEO):
            initial["kind"] = kind
        form = MaterialLinkForm(request.POST or None, meeting=meeting, initial=initial, instance=Material(meeting=meeting))
    if request.method == "POST" and form.is_valid():
        material = form.save(commit=False)
        services.add_material(material, request.user, send_notifications=request.POST.get("silent") != "1")
        messages.success(request, "Материал добавлен.")
        return _back(meeting, "records" if material.kind == Material.Kind.AUDIO else "materials")
    return render(request, "meetings/material_form.html", {"form": form, "meeting": meeting, "kind": kind})


@login_required
@require_POST
def material_delete(request, material_pk):
    material = get_object_or_404(Material.objects.select_related("meeting__commission"), pk=material_pk)
    meeting = material.meeting
    _require_manage(request.user, meeting)
    kind = material.kind
    services.delete_material(material)
    messages.success(request, "Материал удалён.")
    return _back(meeting, "records" if kind == Material.Kind.AUDIO else "materials")


@login_required
def material_download(request, material_pk):
    material = get_object_or_404(Material.objects.select_related("meeting__commission"), pk=material_pk)
    if not can_view_meeting(request.user, material.meeting):
        raise PermissionDenied("Нет доступа к материалам заседания")
    if material.kind != Material.Kind.FILE or not material.file:
        if material.url:
            return redirect(material.url)
        raise Http404
    return serve_file(material.file, material.file_name or None, inline=request.GET.get("download") != "1")


# ---------- участники ----------

@login_required
def attendance(request, pk):
    meeting = get_meeting_or_403(request.user, pk, manage=True)
    rows = services.attendance_rows(meeting)
    reasons = list(AbsenceReason.objects.all())
    reasons_by_pk = {r.pk: r for r in reasons}
    if request.method == "POST":
        values = {}
        for row in rows:
            uid = row["user"].pk
            status = request.POST.get(f"status_{uid}", "")
            reason = reasons_by_pk.get(_int(request.POST.get(f"reason_{uid}")))
            values[uid] = (status, reason)
        try:
            quorum = services.save_attendance(meeting, values)
        except ValidationError as exc:
            messages.error(request, "; ".join(exc.messages))
        else:
            msg = f"Присутствие сохранено. Кворум: {quorum['present']} из {quorum['total']} (нужно {quorum['needed']}) — "
            msg += "достигнут." if quorum["reached"] else "НЕ достигнут."
            (messages.success if quorum["reached"] else messages.warning)(request, msg)
            return redirect(f"{reverse('meetings:attendance', args=[meeting.pk])}")
    return render(request, "meetings/attendance.html", {
        "meeting": meeting, "rows": rows, "reasons": reasons, "statuses": Attendance.Status.choices,
        "quorum": meeting.quorum(),
    })


@login_required
def invitation_add(request, pk):
    meeting = get_meeting_or_403(request.user, pk, manage=True)
    form = InvitationForm(request.POST or None, meeting=meeting, instance=Invitation(meeting=meeting))
    if request.method == "POST" and form.is_valid():
        services.add_invitation(form.save(commit=False), request.user)
        messages.success(request, "Участник приглашён и уведомлён.")
        return _back(meeting, "participants")
    return render(request, "meetings/simple_form.html", {
        "form": form, "meeting": meeting, "title": "Пригласить участника", "submit": "Пригласить",
        "back_tab": "participants",
        "note": "Приглашённый видит только это заседание: повестку, материалы и протоколы — до указанной даты.",
    })


@login_required
@require_POST
def invitation_delete(request, invitation_pk):
    invitation = get_object_or_404(Invitation.objects.select_related("meeting__commission"), pk=invitation_pk)
    meeting = invitation.meeting
    _require_manage(request.user, meeting)
    invitation.delete()
    messages.success(request, "Приглашение отозвано.")
    return _back(meeting, "participants")


# ---------- записи и транскрипты ----------

@login_required
@require_POST
def transcribe(request, material_pk):
    material = get_object_or_404(Material.objects.select_related("meeting__commission"), pk=material_pk, kind=Material.Kind.AUDIO)
    meeting = material.meeting
    _require_manage(request.user, meeting)
    try:
        t = transcription.run_for_material(material, request.user)
    except transcription.TranscriptionNotConfigured:
        messages.warning(
            request,
            "Сервис автоматической транскрипции не настроен. Обратитесь к администратору "
            "или внесите текст вручную.",
        )
    else:
        if t.status == Transcript.Status.DONE:
            messages.success(request, "Транскрипт готов. Проверьте текст перед использованием в протоколе.")
        else:
            messages.error(request, f"Не удалось распознать запись: {t.error}")
    return _back(meeting, "records")


@login_required
def transcript_manual(request, pk):
    meeting = get_meeting_or_403(request.user, pk, manage=True)
    initial = {}
    if request.GET.get("audio"):
        initial["audio"] = _int(request.GET["audio"])
    form = TranscriptForm(request.POST or None, meeting=meeting, initial=initial,
                          instance=Transcript(meeting=meeting, status=Transcript.Status.MANUAL))
    if request.method == "POST" and form.is_valid():
        t = form.save(commit=False)
        t.created_by = request.user
        t.status = Transcript.Status.MANUAL
        t.save()
        messages.success(request, "Текст записи сохранён.")
        return _back(meeting, "records")
    return render(request, "meetings/transcript_form.html", {"form": form, "meeting": meeting, "title": "Текст записи заседания"})


@login_required
def transcript_edit(request, transcript_pk):
    transcript = get_object_or_404(Transcript.objects.select_related("meeting__commission"), pk=transcript_pk)
    meeting = transcript.meeting
    _require_manage(request.user, meeting)
    form = TranscriptForm(request.POST or None, meeting=meeting, instance=transcript)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, "Транскрипт сохранён.")
        return _back(meeting, "records")
    return render(request, "meetings/transcript_form.html", {
        "form": form, "meeting": meeting, "transcript": transcript, "title": "Редактирование транскрипта",
    })


@login_required
def transcript_view(request, transcript_pk):
    transcript = get_object_or_404(Transcript.objects.select_related("meeting__commission", "audio"), pk=transcript_pk)
    _require_commission_view(request.user, transcript.meeting)
    return render(request, "meetings/transcript_view.html", {
        "transcript": transcript, "meeting": transcript.meeting,
        "can_manage": commission_access.can_manage(request.user, transcript.meeting.commission),
    })


@login_required
@require_POST
def transcript_delete(request, transcript_pk):
    transcript = get_object_or_404(Transcript.objects.select_related("meeting__commission"), pk=transcript_pk)
    meeting = transcript.meeting
    _require_manage(request.user, meeting)
    transcript.delete()
    messages.success(request, "Транскрипт удалён.")
    return _back(meeting, "records")


# ---------- график ----------

@login_required
def schedule_view(request, commission_pk):
    commission = commission_access.get_commission_or_403(request.user, commission_pk, manage=True)
    today = timezone.localdate()
    initial = {
        "year": _int(request.GET.get("year"), today.year + (1 if today.month >= 11 else 0)),
        "rule": "nth_weekday", "nth": 2, "weekday": 3, "quarter_month": 1, "format": Meeting.Format.OFFLINE,
        "start_time": "14:00", "duration_minutes": 90,
    }
    form = ScheduleForm(request.POST or None, initial=initial)
    slots, description = None, ""
    if request.method == "POST" and form.is_valid():
        from . import schedule

        d = form.cleaned_data
        try:
            dates = form.dates()
        except ValueError as exc:
            form.add_error(None, str(exc))
            dates = None
        if dates is not None:
            slots = schedule.build_slots(commission, dates, d["start_time"])
            description = form.describe()
            if request.POST.get("action") == "create":
                created = 0
                for slot in slots:
                    if not slot.will_create:
                        continue
                    meeting = Meeting(
                        commission=commission, kind=Meeting.Kind.PLANNED, starts_at=slot.starts_at,
                        duration_minutes=d["duration_minutes"], format=d["format"], place=d.get("place", ""),
                        video_link=d.get("video_link", ""),
                    )
                    services.create_meeting(meeting, request.user, send_notifications=False, control_item=False)
                    meeting.cities.set(commission.cities.all())
                    created += 1
                if created:
                    from apps.notifications.models import Event
                    from apps.notifications.services import notify

                    notify(
                        [r.user for r in commission.members_on()], Event.MEETING_SCHEDULED,
                        f"{commission.short_name}: утверждён график заседаний на {d['year']} год",
                        f"В календарь добавлено заседаний: {created} ({description}).",
                        reverse("meetings:calendar") + f"?view=year&year={d['year']}&commission={commission.pk}",
                        commission,
                    )
                messages.success(request, f"Создано заседаний: {created}. Даты, где заседание уже есть или прошли, пропущены.")
                return redirect(reverse("meetings:calendar") + f"?view=year&year={d['year']}&commission={commission.pk}")
    return render(request, "meetings/schedule.html", {
        "commission": commission, "form": form, "slots": slots, "description": description,
        "to_create": sum(1 for s in slots if s.will_create) if slots else 0,
    })


# ---------- экспорт ----------

@login_required
def agenda_docx(request, pk):
    meeting = get_meeting_or_403(request.user, pk)
    data, filename = docgen.agenda_docx(meeting)
    from urllib.parse import quote

    response = HttpResponse(data, content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document")
    response["Content-Disposition"] = f"attachment; filename=\"agenda.docx\"; filename*=UTF-8''{quote(filename)}"
    return response


@login_required
def meeting_ics(request, pk):
    meeting = get_meeting_or_403(request.user, pk)
    return ical.ics_response([meeting], f"meeting-{meeting.pk}.ics", str(meeting))


@login_required
def commission_ics(request, commission_pk):
    commission = commission_access.get_commission_or_403(request.user, commission_pk)
    year = _int(request.GET.get("year"), timezone.localdate().year)
    meetings = (
        Meeting.objects.filter(commission=commission, starts_at__year=year)
        .select_related("commission").order_by("starts_at")
    )
    return ical.ics_response(meetings, f"{commission.pk}-{year}.ics", f"{commission.short_name} — заседания {year}")


@login_required
def my_ics(request):
    user = request.user
    today = timezone.localdate()
    from apps.committees.models import MemberRecord

    commission_ids = set(
        MemberRecord.objects.filter(user=user, start_date__lte=today)
        .filter(Q(end_date__isnull=True) | Q(end_date__gt=today))
        .values_list("commission_id", flat=True)
    )
    invited = Invitation.objects.filter(user=user, valid_until__gte=today).values_list("meeting_id", flat=True)
    meetings = (
        Meeting.objects.filter(Q(commission_id__in=commission_ids) | Q(pk__in=list(invited)))
        .filter(starts_at__gte=timezone.now() - timedelta(days=90), starts_at__lte=timezone.now() + timedelta(days=400))
        .select_related("commission").order_by("starts_at")
    )
    return ical.ics_response(meetings, "my-meetings.ics", "Мои заседания")
