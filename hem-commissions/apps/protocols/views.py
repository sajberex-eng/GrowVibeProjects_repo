"""Протоколы, согласование, подпись, особые мнения, поручения, публичная проверка."""
import hashlib

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.paginator import Paginator
from django.db.models import Max, Q
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from apps.accounts.models import User
from apps.audit.middleware import client_ip
from apps.committees import access
from apps.committees.models import MemberRecord
from apps.committees.services import serve_file
from apps.meetings.access import get_meeting_or_403, is_invited
from apps.meetings.models import Transcript

from . import docgen, services
from .forms import (
    ApprovalResponseForm,
    AssignmentForm,
    CodeForm,
    CommentForm,
    ConfirmPatientIdsForm,
    DecisionForm,
    DissentForm,
    ProtocolHeaderForm,
    ProtocolItemForm,
    ReasonForm,
    ReportDoneForm,
    VerifyUploadForm,
)
from .models import Assignment, Decision, DissentingOpinion, Protocol, ProtocolApproval, ProtocolItem

PAGE_SIZE = 25


# ---------------------------------------------------------------- доступ


def can_view_protocol(user, protocol):
    if not user.is_authenticated:
        return False
    if access.can_view(user, protocol.commission):
        return True
    return (
        protocol.meeting_id is not None
        and protocol.status == Protocol.Status.SIGNED
        and is_invited(user, protocol.meeting)
    )


def get_protocol(user, pk, manage=False):
    protocol = get_object_or_404(Protocol.objects.select_related("commission", "meeting"), pk=pk)
    if manage:
        access.require(access.can_manage(user, protocol.commission), "Нет прав на ведение протоколов комиссии")
    elif not can_view_protocol(user, protocol):
        raise PermissionDenied("Нет доступа к протоколу")
    return protocol


def get_editable_protocol(request, pk):
    protocol = get_protocol(request.user, pk, manage=True)
    if not protocol.is_editable:
        raise PermissionDenied("Протокол можно изменять только в статусе «Черновик»")
    return protocol


def _error(request, exc):
    for msg in getattr(exc, "messages", [str(exc)]):
        messages.error(request, msg)


def _back(protocol, anchor=""):
    return redirect(f"{protocol_url(protocol)}{anchor}")


def protocol_url(protocol):
    return services.protocol_url(protocol)


def _pdf_response(data, name, inline=True):
    response = HttpResponse(data, content_type="application/pdf")
    disposition = "inline" if inline else "attachment"
    response["Content-Disposition"] = f'{disposition}; filename="{name}"'
    response["X-Content-Type-Options"] = "nosniff"
    return response


# ---------------------------------------------------------------- список, создание


@login_required
def protocol_list(request):
    user = request.user
    commissions = list(access.visible_commissions(user))
    ids = [c.pk for c in commissions]
    invited_meetings = list(
        user.invitations.filter(valid_until__gte=timezone.localdate()).values_list("meeting_id", flat=True)
    )
    qs = Protocol.objects.filter(
        Q(commission_id__in=ids) | Q(meeting_id__in=invited_meetings, status=Protocol.Status.SIGNED)
    ).select_related("commission", "meeting")
    f = request.GET
    if f.get("commission"):
        qs = qs.filter(commission_id=f["commission"]) if f["commission"].isdigit() else qs.none()
    if f.get("status") in Protocol.Status.values:
        qs = qs.filter(status=f["status"])
    if f.get("year", "").isdigit():
        qs = qs.filter(protocol_date__year=int(f["year"]))
    if f.get("kind") in Protocol.Kind.values:
        qs = qs.filter(kind=f["kind"])
    q = f.get("q", "").strip()
    if q:
        qs = qs.filter(
            Q(number__icontains=q) | Q(items__title__icontains=q) | Q(items__resolved__icontains=q)
            | Q(decisions__text__icontains=q) | Q(items__patient_id__iexact=q)
        ).distinct()
    years = sorted({d.year for d in Protocol.objects.filter(commission_id__in=ids).dates("protocol_date", "year")}, reverse=True)
    page = Paginator(qs.order_by("-protocol_date", "-id"), PAGE_SIZE).get_page(f.get("page"))
    managed = [c for c in commissions if access.can_manage(user, c)]
    return render(request, "protocols/list.html", {
        "page": page, "commissions": commissions, "managed": managed, "years": years,
        "statuses": Protocol.Status.choices, "kinds": Protocol.Kind.choices, "f": f,
    })


@login_required
def create_from_meeting(request, meeting_pk):
    meeting = get_meeting_or_403(request.user, meeting_pk, manage=True)
    transcripts = meeting.transcripts.filter(status__in=[Transcript.Status.DONE, Transcript.Status.MANUAL]).order_by("-created_at")
    existing = meeting.protocols.exclude(status=Protocol.Status.ANNULLED)
    if request.method == "POST":
        transcript = None
        tid = request.POST.get("transcript", "")
        if tid:
            transcript = get_object_or_404(transcripts, pk=tid)
        protocol = services.create_draft_from_meeting(meeting, request.user, transcript)
        messages.success(request, f"Создан черновик протокола № {protocol.number}.")
        if services.has_flagged_names(protocol):
            messages.warning(request, "В тексте найдены возможные ФИО. Проверьте и замените их на ID пациентов из МИС.")
        return redirect("protocols:detail", protocol.pk)
    return render(request, "protocols/create_from_meeting.html", {
        "meeting": meeting, "transcripts": transcripts, "existing": existing,
    })


@login_required
def create_absentee(request, commission_pk):
    from apps.voting.models import VotingSession

    commission = access.get_commission_or_403(request.user, commission_pk, manage=True)
    used = Protocol.objects.exclude(status=Protocol.Status.ANNULLED).values_list("votings", flat=True)
    sessions = (
        VotingSession.objects.filter(commission=commission, status=VotingSession.Status.CLOSED, meeting__isnull=True)
        .exclude(pk__in=[x for x in used if x])
        .order_by("-closed_at")
    )
    if request.method == "POST":
        chosen = list(sessions.filter(pk__in=request.POST.getlist("sessions")))
        try:
            protocol = services.create_absentee_protocol(chosen, request.user)
        except ValidationError as exc:
            _error(request, exc)
        else:
            messages.success(request, f"Создан протокол заочного голосования № {protocol.number}.")
            return redirect("protocols:detail", protocol.pk)
    return render(request, "protocols/create_absentee.html", {"commission": commission, "sessions": sessions})


# ---------------------------------------------------------------- карточка


STEPS = [
    (Protocol.Status.DRAFT, "Черновик"),
    (Protocol.Status.APPROVAL, "Согласование"),
    (Protocol.Status.SIGNING, "Подписание"),
    (Protocol.Status.SIGNED, "Подписан"),
]


@login_required
def protocol_detail(request, pk):
    user = request.user
    protocol = get_protocol(user, pk)
    commission = protocol.commission
    roles = access.roles_in(user, commission)
    can_manage = access.can_manage(user, commission)
    member_view = access.can_view(user, commission)
    items = list(
        protocol.items.order_by("position", "id").prefetch_related(
            "decisions__assignments__responsible", "decisions__assignments__co_executors"
        )
    )
    dissents = list(protocol.dissents.select_related("author", "item").order_by("item__position", "created_at"))
    active = [d for d in dissents if d.withdrawn_at is None and d.signed_at]
    for item in items:
        item.active_dissents = [d for d in dissents if d.item_id == item.pk and d.withdrawn_at is None]
        item.dissent_note = services.dissent_note(
            list(dict.fromkeys(d.author.full_name for d in active if d.item_id == item.pk))
        )

    approvals = list(services.current_approvals(protocol).order_by("user__last_name"))
    my_approval = services.pending_approval_for(protocol, user)
    approval_open = services.approval_open(protocol)
    waiting = [a for a in approvals if a.status == ProtocolApproval.Status.PENDING]
    old_approvals = (
        protocol.approvals.exclude(revision=protocol.revision).exclude(status=ProtocolApproval.Status.PENDING)
        .select_related("user", "item").order_by("-revision", "user__last_name")
        if can_manage else []
    )

    quorum = None
    attendance = None
    if protocol.meeting_id and member_view:
        quorum = protocol.meeting.quorum()
        attendance = protocol.meeting.attendance.select_related("user", "reason").order_by("user__last_name")

    signatures = list(protocol.signatures.select_related("user").order_by("signed_at"))
    next_role, next_signer = services.expected_signer(protocol) if protocol.status == Protocol.Status.SIGNING else (None, None)
    is_chairman_officer = (
        commission.officer(MemberRecord.Role.CHAIRMAN, protocol.protocol_date) == user
    )
    can_dissent = (
        approval_open and member_view
        and access.is_voting_member(user, commission, protocol.protocol_date)
    )
    current = [s for s, _ in STEPS]
    step_index = current.index(protocol.status) if protocol.status in current else len(STEPS)
    ctx = {
        "protocol": protocol,
        "commission": commission,
        "items": items,
        "dissents": dissents,
        "my_dissents": [d for d in dissents if d.author_id == user.pk and d.withdrawn_at is None],
        "approvals": approvals if can_manage else [a for a in approvals if a.user_id == user.pk],
        "old_approvals": old_approvals,
        "waiting": waiting,
        "answered_count": len(approvals) - len(waiting),
        "my_approval": my_approval,
        "approval_open": approval_open,
        "response_form": ApprovalResponseForm(protocol=protocol, initial={"status": my_approval.status if my_approval and my_approval.status != "pending" else None}),
        "dissent_form": DissentForm(protocol=protocol) if can_dissent else None,
        "can_dissent": can_dissent,
        "can_manage": can_manage,
        "member_view": member_view,
        "roles": roles,
        "quorum": quorum,
        "attendance": attendance,
        "signatures": signatures,
        "next_role": next_role,
        "next_role_label": MemberRecord.Role(next_role).label if next_role else "",
        "next_signer": next_signer,
        "can_sign": services.can_sign(protocol, user),
        "can_return": protocol.status == Protocol.Status.SIGNING and is_chairman_officer,
        "can_annul": services.can_annul(protocol, user),
        "needs_deid": services.requires_deidentification(protocol),
        "has_flags": any(i.flagged_names for i in items),
        "confirm_form": ConfirmPatientIdsForm(initial={"confirmed": protocol.patient_ids_confirmed}),
        "code_form": CodeForm(),
        "reason_form": ReasonForm(),
        "steps": [(label, i < step_index, i == step_index) for i, (_, label) in enumerate(STEPS)],
        "replaced_by": protocol.replaced_by.all(),
        "today": timezone.localdate(),
    }
    return render(request, "protocols/detail.html", ctx)


# ---------------------------------------------------------------- редактирование


@login_required
def protocol_edit(request, pk):
    protocol = get_editable_protocol(request, pk)
    form = ProtocolHeaderForm(request.POST or None, instance=protocol)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, "Реквизиты протокола сохранены.")
        return _back(protocol)
    return render(request, "protocols/form.html", {
        "protocol": protocol, "form": form, "title": "Реквизиты протокола",
    })


@login_required
@require_POST
def item_add(request, pk):
    protocol = get_editable_protocol(request, pk)
    last = protocol.items.aggregate(m=Max("position"))["m"] or 0
    item = ProtocolItem.objects.create(protocol=protocol, position=last + 1, title="Новый вопрос")
    return redirect("protocols:item_edit", item.pk)


def _get_item(request, item_pk):
    item = get_object_or_404(ProtocolItem.objects.select_related("protocol"), pk=item_pk)
    get_editable_protocol(request, item.protocol_id)
    return item


@login_required
def item_edit(request, item_pk):
    item = _get_item(request, item_pk)
    protocol = item.protocol
    form = ProtocolItemForm(request.POST or None, instance=item, commission=protocol.commission)
    if request.method == "POST" and form.is_valid():
        item = form.save(commit=False)
        services.refresh_flags(item)
        item.save()
        if item.flagged_names and protocol.commission.handles_patient_cases:
            protocol.patient_ids_confirmed = False
            protocol.save(update_fields=["patient_ids_confirmed", "updated_at"])
            messages.warning(request, "Найдены возможные ФИО: " + "; ".join(item.flagged_names))
        messages.success(request, "Вопрос сохранён.")
        return _back(protocol, f"#item-{item.pk}")
    return render(request, "protocols/form.html", {
        "protocol": protocol, "form": form, "title": f"Вопрос {item.position}", "item": item,
    })


@login_required
@require_POST
def item_delete(request, item_pk):
    item = _get_item(request, item_pk)
    protocol = item.protocol
    item.delete()
    services.renumber_items(protocol)
    messages.success(request, "Вопрос удалён.")
    return _back(protocol)


@login_required
@require_POST
def item_move(request, item_pk):
    item = _get_item(request, item_pk)
    protocol = item.protocol
    services.renumber_items(protocol)
    item.refresh_from_db()
    delta = -1 if request.POST.get("direction") == "up" else 1
    other = protocol.items.filter(position=item.position + delta).first()
    if other:
        other.position, item.position = item.position, other.position
        other.save(update_fields=["position"])
        item.save(update_fields=["position"])
    return _back(protocol, f"#item-{item.pk}")


@login_required
def decision_add(request, item_pk):
    item = _get_item(request, item_pk)
    count = item.decisions.count() + 1
    form = DecisionForm(request.POST or None, initial={"number": f"{item.position}.{count}"})
    if request.method == "POST" and form.is_valid():
        decision = form.save(commit=False)
        decision.protocol = item.protocol
        decision.item = item
        decision.save()
        messages.success(request, "Решение добавлено.")
        return _back(item.protocol, f"#item-{item.pk}")
    return render(request, "protocols/form.html", {
        "protocol": item.protocol, "form": form, "title": f"Решение по вопросу {item.position}",
    })


def _get_decision(request, pk):
    decision = get_object_or_404(Decision.objects.select_related("protocol", "item"), pk=pk)
    get_editable_protocol(request, decision.protocol_id)
    return decision


@login_required
def decision_edit(request, pk):
    decision = _get_decision(request, pk)
    form = DecisionForm(request.POST or None, instance=decision)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, "Решение сохранено.")
        return _back(decision.protocol, f"#item-{decision.item_id}")
    return render(request, "protocols/form.html", {
        "protocol": decision.protocol, "form": form, "title": "Решение", "delete_url_name": "protocols:decision_delete", "obj": decision,
    })


@login_required
@require_POST
def decision_delete(request, pk):
    decision = _get_decision(request, pk)
    protocol = decision.protocol
    decision.delete()
    messages.success(request, "Решение удалено.")
    return _back(protocol)


@login_required
def assignment_add(request, decision_pk):
    decision = _get_decision(request, decision_pk)
    form = AssignmentForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        assignment = form.save(commit=False)
        assignment.decision = decision
        assignment.save()
        form.save_m2m()
        messages.success(request, "Поручение добавлено. Исполнители получат уведомление после подписания протокола.")
        return _back(decision.protocol, f"#item-{decision.item_id}")
    return render(request, "protocols/form.html", {
        "protocol": decision.protocol, "form": form, "title": "Поручение",
    })


def _get_draft_assignment(request, pk):
    assignment = get_object_or_404(Assignment.objects.select_related("decision__protocol"), pk=pk)
    get_editable_protocol(request, assignment.decision.protocol_id)
    return assignment


@login_required
def assignment_edit(request, pk):
    assignment = _get_draft_assignment(request, pk)
    form = AssignmentForm(request.POST or None, instance=assignment)
    protocol = assignment.decision.protocol
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, "Поручение сохранено.")
        return _back(protocol, f"#item-{assignment.decision.item_id}")
    return render(request, "protocols/form.html", {
        "protocol": protocol, "form": form, "title": "Поручение", "delete_url_name": "protocols:assignment_delete", "obj": assignment,
    })


@login_required
@require_POST
def assignment_delete(request, pk):
    assignment = _get_draft_assignment(request, pk)
    protocol = assignment.decision.protocol
    assignment.delete()
    messages.success(request, "Поручение удалено.")
    return _back(protocol)


# ---------------------------------------------------------------- жизненный цикл


@login_required
@require_POST
def confirm_patient_ids(request, pk):
    protocol = get_editable_protocol(request, pk)
    form = ConfirmPatientIdsForm(request.POST)
    form.is_valid()
    protocol.patient_ids_confirmed = bool(form.cleaned_data.get("confirmed"))
    protocol.save(update_fields=["patient_ids_confirmed", "updated_at"])
    if protocol.patient_ids_confirmed:
        from apps.audit.services import log

        log("protocol_deidentification_confirmed", protocol)
        messages.success(request, "Обезличивание подтверждено.")
    return _back(protocol)


@login_required
@require_POST
def send_to_approval(request, pk):
    protocol = get_protocol(request.user, pk, manage=True)
    try:
        services.send_to_approval(protocol, request.user)
    except ValidationError as exc:
        _error(request, exc)
    else:
        messages.success(request, f"Проект направлен на согласование до {protocol.approval_deadline:%d.%m.%Y}.")
    return _back(protocol)


@login_required
@require_POST
def respond_approval(request, pk):
    protocol = get_protocol(request.user, pk)
    form = ApprovalResponseForm(request.POST, protocol=protocol)
    if not form.is_valid():
        for errors in form.errors.values():
            for e in errors:
                messages.error(request, e)
        return _back(protocol, "#approval")
    try:
        services.respond_approval(
            protocol, request.user, form.cleaned_data["status"], form.cleaned_data.get("remarks", ""),
            form.cleaned_data.get("item"),
        )
    except ValidationError as exc:
        _error(request, exc)
    else:
        messages.success(request, "Ответ по согласованию сохранён.")
    return _back(protocol, "#approval")


@login_required
@require_POST
def new_revision(request, pk):
    protocol = get_protocol(request.user, pk, manage=True)
    try:
        services.new_revision(protocol, request.user)
    except ValidationError as exc:
        _error(request, exc)
    else:
        messages.success(request, "Протокол возвращён в черновик. После правок отправьте новую редакцию на согласование.")
    return _back(protocol)


@login_required
@require_POST
def move_to_signing(request, pk):
    protocol = get_protocol(request.user, pk, manage=True)
    try:
        services.move_to_signing(protocol, request.user)
    except ValidationError as exc:
        _error(request, exc)
    else:
        messages.success(request, "Протокол сформирован и передан на подпись.")
    return _back(protocol)


@login_required
@require_POST
def request_sign_code(request, pk):
    protocol = get_protocol(request.user, pk)
    try:
        services.request_sign_code(protocol, request.user)
    except ValidationError as exc:
        _error(request, exc)
    else:
        messages.success(request, f"Код подтверждения отправлен на {request.user.email}.")
    return _back(protocol, "#signing")


@login_required
@require_POST
def sign(request, pk):
    protocol = get_protocol(request.user, pk)
    form = CodeForm(request.POST)
    if not form.is_valid():
        messages.error(request, "Введите 6-значный код из письма.")
        return _back(protocol, "#signing")
    try:
        services.sign(protocol, request.user, form.cleaned_data["code"], client_ip(request))
    except ValidationError as exc:
        _error(request, exc)
    else:
        protocol.refresh_from_db()
        if protocol.status == Protocol.Status.SIGNED:
            messages.success(request, "Протокол подписан. Сформирован итоговый документ.")
        else:
            messages.success(request, "Подпись поставлена. Протокол передан председателю.")
    return _back(protocol, "#signing")


@login_required
@require_POST
def return_for_rework(request, pk):
    protocol = get_protocol(request.user, pk)
    try:
        services.return_for_rework(protocol, request.user, request.POST.get("reason", ""))
    except ValidationError as exc:
        _error(request, exc)
    else:
        messages.success(request, "Протокол возвращён на доработку, подписи отозваны.")
    return _back(protocol)


@login_required
@require_POST
def annul(request, pk):
    protocol = get_protocol(request.user, pk)
    try:
        services.annul(protocol, request.user, request.POST.get("reason", ""))
    except ValidationError as exc:
        _error(request, exc)
    else:
        messages.success(request, "Протокол аннулирован. Открытые поручения по нему сняты.")
    return _back(protocol)


@login_required
@require_POST
def create_replacement(request, pk):
    protocol = get_protocol(request.user, pk, manage=True)
    try:
        new = services.create_replacement(protocol, request.user)
    except ValidationError as exc:
        _error(request, exc)
        return _back(protocol)
    messages.success(request, f"Создан черновик № {new.number} взамен аннулированного протокола.")
    return redirect("protocols:detail", new.pk)


# ---------------------------------------------------------------- файлы


@login_required
def download_frozen(request, pk):
    protocol = get_protocol(request.user, pk)
    if not protocol.frozen_pdf:
        raise PermissionDenied("Документ на подпись ещё не сформирован")
    return serve_file(protocol.frozen_pdf, docgen.filename(protocol, "pdf"))


@login_required
def download_signed(request, pk):
    protocol = get_protocol(request.user, pk)
    if not protocol.signed_pdf:
        raise PermissionDenied("Подписанный документ ещё не сформирован")
    return serve_file(protocol.signed_pdf, docgen.filename(protocol, "pdf").replace(".pdf", "_signed.pdf"))


@login_required
def preview_pdf(request, pk):
    protocol = get_protocol(request.user, pk, manage=True)
    return _pdf_response(services.draft_pdf(protocol), docgen.filename(protocol, "pdf").replace(".pdf", "_draft.pdf"))


@login_required
def export_docx(request, pk):
    protocol = get_protocol(request.user, pk)
    if protocol.snapshot and protocol.status != Protocol.Status.DRAFT:
        snap = protocol.snapshot
    else:
        access.require(access.can_manage(request.user, protocol.commission), "Проект протокола доступен только секретарю и председателю")
        snap = services.build_snapshot(protocol, draft=True)
    data = docgen.export_docx(protocol, snap)
    response = HttpResponse(data, content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document")
    response["Content-Disposition"] = f'attachment; filename="{docgen.filename(protocol, "docx")}"'
    return response


# ---------------------------------------------------------------- особые мнения


@login_required
@require_POST
def dissent_code(request, pk):
    protocol = get_protocol(request.user, pk)
    try:
        services.request_dissent_code(protocol, request.user)
    except ValidationError as exc:
        _error(request, exc)
    else:
        messages.success(request, f"Код подтверждения отправлен на {request.user.email}. Заполните форму особого мнения.")
    return _back(protocol, "#dissent")


@login_required
@require_POST
def dissent_add(request, pk):
    protocol = get_protocol(request.user, pk)
    form = DissentForm(request.POST, request.FILES, protocol=protocol)
    if not form.is_valid():
        for errors in form.errors.values():
            for e in errors:
                messages.error(request, e)
        return _back(protocol, "#dissent")
    data = form.cleaned_data
    try:
        services.add_dissent(protocol, request.user, data["item"], data["text"], data.get("file"), data["code"], client_ip(request))
    except ValidationError as exc:
        _error(request, exc)
    else:
        messages.success(request, "Особое мнение подписано и включено в проект протокола.")
    return _back(protocol, "#dissent")


@login_required
@require_POST
def dissent_withdraw(request, pk):
    dissent = get_object_or_404(DissentingOpinion.objects.select_related("protocol"), pk=pk)
    get_protocol(request.user, dissent.protocol_id)
    try:
        services.withdraw_dissent(dissent, request.user)
    except ValidationError as exc:
        _error(request, exc)
    else:
        messages.success(request, "Особое мнение отозвано.")
    return _back(dissent.protocol, "#dissent")


@login_required
def dissent_file(request, pk):
    dissent = get_object_or_404(DissentingOpinion, pk=pk)
    get_protocol(request.user, dissent.protocol_id)
    if not dissent.file:
        raise PermissionDenied("Файл не приложен")
    return serve_file(dissent.file, dissent.file_name or None)


# ---------------------------------------------------------------- поручения


def can_view_assignment(user, assignment):
    return access.can_view(user, assignment.commission) or services.is_executor(assignment, user)


@login_required
def assignment_list(request):
    user = request.user
    commissions = list(access.visible_commissions(user))
    ids = [c.pk for c in commissions]
    today = timezone.localdate()
    qs = (
        Assignment.objects.filter(Q(commission_id__in=ids) | Q(responsible=user) | Q(co_executors=user))
        .exclude(decision__protocol__status=Protocol.Status.DRAFT)
        .exclude(decision__protocol__status=Protocol.Status.APPROVAL)
        .exclude(decision__protocol__status=Protocol.Status.SIGNING)
        .select_related("commission", "responsible", "decision__protocol")
        .distinct()
    )
    f = request.GET
    if f.get("commission", "").isdigit():
        qs = qs.filter(commission_id=f["commission"])
    if f.get("responsible", "").isdigit():
        qs = qs.filter(Q(responsible_id=f["responsible"]) | Q(co_executors=f["responsible"]))
    status = f.get("status", "")
    if status == "overdue":
        qs = qs.filter(status__in=Assignment.OPEN_STATUSES, due_date__lt=today)
    elif status == "open":
        qs = qs.filter(status__in=Assignment.OPEN_STATUSES)
    elif status in Assignment.Status.values:
        qs = qs.filter(status=status)
    for key, lookup in [("due_from", "due_date__gte"), ("due_to", "due_date__lte")]:
        value = f.get(key, "")
        if value:
            try:
                qs = qs.filter(**{lookup: value})
            except ValidationError:
                pass
    if f.get("mine") == "1":
        qs = qs.filter(Q(responsible=user) | Q(co_executors=user))
    q = f.get("q", "").strip()
    if q:
        qs = qs.filter(Q(text__icontains=q) | Q(responsible_name__icontains=q) | Q(decision__protocol__number__icontains=q))
    page = Paginator(qs.order_by("due_date", "id"), PAGE_SIZE).get_page(f.get("page"))
    responsible_ids = Assignment.objects.filter(commission_id__in=ids).values_list("responsible_id", flat=True)
    people = User.objects.filter(pk__in=[x for x in responsible_ids if x]).order_by("last_name", "first_name")
    status_choices = [("open", "Все открытые"), ("overdue", "Просрочено")] + list(Assignment.Status.choices)
    return render(request, "protocols/assignments.html", {
        "page": page, "commissions": commissions, "people": people, "status_choices": status_choices,
        "f": f, "today": today,
    })


def _get_assignment(request, pk):
    assignment = get_object_or_404(
        Assignment.objects.select_related("commission", "responsible", "decision__protocol", "decision__item"), pk=pk
    )
    if not can_view_assignment(request.user, assignment):
        raise PermissionDenied("Нет доступа к поручению")
    return assignment


@login_required
def assignment_detail(request, pk):
    assignment = _get_assignment(request, pk)
    user = request.user
    protocol = assignment.decision.protocol
    is_exec = services.is_executor(assignment, user)
    can_confirm = services.can_confirm(assignment, user)
    active = protocol.status == Protocol.Status.SIGNED
    labels = dict(Assignment.Status.choices)
    comments = list(assignment.comments.select_related("author"))
    for c in comments:
        c.status_label = labels.get(c.status_change, c.status_change)
    return render(request, "protocols/assignment_detail.html", {
        "a": assignment,
        "protocol": protocol,
        "can_view_protocol": can_view_protocol(user, protocol),
        "comments": comments,
        "co_executors": assignment.co_executors.all(),
        "is_exec": is_exec,
        "can_confirm": can_confirm,
        "can_start": active and assignment.status == Assignment.Status.ASSIGNED and (is_exec or can_confirm),
        "can_report": active and is_exec and assignment.status in (Assignment.Status.ASSIGNED, Assignment.Status.IN_PROGRESS),
        "can_review": can_confirm and assignment.status == Assignment.Status.ON_CONFIRMATION,
        "can_cancel": bool(access.roles_in(user, assignment.commission) & {access.ADMIN, access.SECRETARY, access.CHAIRMAN}) and assignment.is_open,
        "report_form": ReportDoneForm(initial={"completed_on": timezone.localdate()}),
        "comment_form": CommentForm(),
        "reason_form": ReasonForm(),
        "active": active,
    })


def _assignment_action(request, pk, action):
    assignment = _get_assignment(request, pk)
    try:
        action(assignment)
    except ValidationError as exc:
        _error(request, exc)
        return False, assignment
    return True, assignment


@login_required
@require_POST
def assignment_start(request, pk):
    ok, a = _assignment_action(request, pk, lambda a: services.start_assignment(a, request.user))
    if ok:
        messages.success(request, "Поручение взято в работу.")
    return redirect("protocols:assignment_detail", a.pk)


@login_required
@require_POST
def assignment_report(request, pk):
    form = ReportDoneForm(request.POST, request.FILES)
    if not form.is_valid():
        _get_assignment(request, pk)
        for errors in form.errors.values():
            for e in errors:
                messages.error(request, e)
        return redirect("protocols:assignment_detail", pk)
    d = form.cleaned_data
    ok, a = _assignment_action(
        request, pk, lambda a: services.report_done(a, request.user, d["text"], d.get("file"), d.get("completed_on"))
    )
    if ok:
        messages.success(request, "Исполнение отмечено и направлено секретарю на подтверждение.")
    return redirect("protocols:assignment_detail", a.pk)


@login_required
@require_POST
def assignment_confirm(request, pk):
    ok, a = _assignment_action(request, pk, lambda a: services.confirm_assignment(a, request.user, request.POST.get("text", "")))
    if ok:
        messages.success(request, f"Исполнение подтверждено: {a.get_status_display().lower()}.")
    return redirect("protocols:assignment_detail", a.pk)


@login_required
@require_POST
def assignment_return(request, pk):
    ok, a = _assignment_action(request, pk, lambda a: services.return_assignment(a, request.user, request.POST.get("text", "")))
    if ok:
        messages.success(request, "Поручение возвращено в работу.")
    return redirect("protocols:assignment_detail", a.pk)


@login_required
@require_POST
def assignment_cancel(request, pk):
    ok, a = _assignment_action(request, pk, lambda a: services.cancel_assignment(a, request.user, request.POST.get("reason", "")))
    if ok:
        messages.success(request, "Поручение снято.")
    return redirect("protocols:assignment_detail", a.pk)


@login_required
@require_POST
def assignment_comment(request, pk):
    form = CommentForm(request.POST, request.FILES)
    if not form.is_valid():
        _get_assignment(request, pk)
        for errors in form.errors.values():
            for e in errors:
                messages.error(request, e)
        return redirect("protocols:assignment_detail", pk)
    ok, a = _assignment_action(
        request, pk, lambda a: services.add_comment(a, request.user, form.cleaned_data["text"], form.cleaned_data.get("file"))
    )
    if ok:
        messages.success(request, "Комментарий добавлен.")
    return redirect("protocols:assignment_detail", a.pk)


@login_required
def assignment_file(request, pk, comment_pk):
    assignment = _get_assignment(request, pk)
    comment = get_object_or_404(assignment.comments, pk=comment_pk)
    if not comment.file:
        raise PermissionDenied("Файл не приложен")
    return serve_file(comment.file, comment.file_name or None)


# ---------------------------------------------------------------- публичная проверка


def _hash_upload(uploaded):
    digest = hashlib.sha256()
    for chunk in uploaded.chunks():
        digest.update(chunk)
    return digest.hexdigest()


def verify(request, uid):
    """Публичная страница проверки подлинности (без входа, без содержания протокола)."""
    protocol = get_object_or_404(Protocol.objects.select_related("commission"), uid=uid)
    result = None
    form = VerifyUploadForm()
    if request.method == "POST":
        form = VerifyUploadForm(request.POST, request.FILES)
        if form.is_valid():
            from django.conf import settings

            uploaded = form.cleaned_data["file"]
            if uploaded.size > settings.MAX_UPLOAD_MB * 1024 * 1024:
                form.add_error("file", "Файл слишком большой.")
            else:
                digest = _hash_upload(uploaded)
                if protocol.signed_hash and digest == protocol.signed_hash:
                    match = "signed"
                elif protocol.frozen_hash and digest == protocol.frozen_hash:
                    match = "frozen"
                else:
                    match = None
                result = {"hash": digest, "match": match}
    visible = protocol.status in (Protocol.Status.SIGNING, Protocol.Status.SIGNED, Protocol.Status.ANNULLED)
    return render(request, "protocols/verify.html", {
        "protocol": protocol,
        "visible": visible,
        "signatures": protocol.active_signatures() if visible else [],
        "form": form,
        "result": result,
    })
