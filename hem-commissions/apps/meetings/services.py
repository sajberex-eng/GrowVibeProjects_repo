"""Бизнес-операции с заседаниями, повесткой, материалами и участниками.

Правило уведомлений: в тексте — только комиссия, дата заседания и ссылка,
без данных пациентов и без формулировок вопросов повестки.
"""
import os

from django.core.exceptions import PermissionDenied, ValidationError
from django.core.files.base import ContentFile
from django.db import transaction
from django.db.models import Max
from django.urls import reverse
from django.utils import timezone

from apps.audit.services import log
from apps.committees import access as commission_access
from apps.committees.models import MemberRecord
from apps.notifications.models import Event
from apps.notifications.services import notify

from .models import (
    AgendaAcknowledgement, AgendaItem, AgendaProposal, Attendance, Invitation, Material, Meeting,
    MeetingReschedule,
)

CONTROL_ITEM_TITLE = "Контроль исполнения ранее принятых решений"


def local_str(dt, fmt="%d.%m.%Y %H:%M"):
    return timezone.localtime(dt).strftime(fmt)


def meeting_url(meeting, tab=None):
    url = reverse("meetings:detail", args=[meeting.pk])
    return f"{url}?tab={tab}" if tab else url


# ---------- получатели ----------

def composition_users(meeting):
    from apps.accounts.models import User

    ids = list(meeting.composition().values_list("user_id", flat=True))
    return list(User.objects.filter(pk__in=ids, is_active=True))


def invited_users(meeting, valid_only=True):
    qs = meeting.invitations.select_related("user")
    if valid_only:
        qs = qs.filter(valid_until__gte=timezone.localdate())
    return [inv.user for inv in qs if inv.user.is_active]


def meeting_recipients(meeting):
    """Состав на дату заседания + действующие приглашённые (без повторов)."""
    seen, result = set(), []
    for user in composition_users(meeting) + invited_users(meeting):
        if user.pk not in seen:
            seen.add(user.pk)
            result.append(user)
    return result


def secretary_for(meeting):
    commission = meeting.commission
    return (
        commission.officer(MemberRecord.Role.SECRETARY, meeting.meeting_date)
        or commission.secretary
    )


def chairman_for(meeting):
    commission = meeting.commission
    return (
        commission.officer(MemberRecord.Role.CHAIRMAN, meeting.meeting_date)
        or commission.chairman
    )


def can_approve_agenda(user, meeting):
    roles = commission_access.roles_in(user, meeting.commission)
    return bool(roles & {commission_access.CHAIRMAN, commission_access.ADMIN})


def can_propose(user, meeting):
    return meeting.is_open_for_proposals and commission_access.is_voting_member(user, meeting.commission)


def can_acknowledge(user, meeting):
    if meeting.agenda_status != Meeting.AgendaStatus.APPROVED or not meeting.is_upcoming:
        return False
    if commission_access.is_voting_member(user, meeting.commission):
        return True
    return meeting.invitations.filter(user=user, valid_until__gte=timezone.localdate()).exists()


# ---------- заседание ----------

def _title(meeting):
    return f"{meeting.commission.short_name}: заседание {local_str(meeting.starts_at)}"


@transaction.atomic
def create_meeting(meeting, user=None, send_notifications=True, control_item=True):
    """Сохраняет новое заседание, формирует пункт контроля исполнения и уведомляет состав."""
    if user is not None and user.is_authenticated:
        meeting.created_by = user
    meeting.save()
    if control_item:
        ensure_control_item(meeting)
    if send_notifications:
        kind = "внеочередное заседание" if meeting.kind == Meeting.Kind.EXTRA else "заседание"
        notify(
            meeting_recipients(meeting), Event.MEETING_SCHEDULED,
            f"Назначено {kind}: {_title(meeting)}",
            _details(meeting), meeting_url(meeting), meeting.commission,
        )
    return meeting


def _details(meeting):
    lines = [
        f"Комиссия: {meeting.commission.name}",
        f"Дата и время: {local_str(meeting.starts_at)}",
        f"Формат: {meeting.get_format_display()}",
    ]
    if meeting.place:
        lines.append(f"Место: {meeting.place}")
    return "\n".join(lines)


@transaction.atomic
def reschedule_meeting(meeting, new_starts_at, reason="", user=None):
    if not meeting.is_upcoming:
        raise ValidationError("Перенести можно только запланированное заседание.")
    if new_starts_at == meeting.starts_at:
        raise ValidationError("Новая дата совпадает с текущей.")
    old = meeting.starts_at
    MeetingReschedule.objects.create(
        meeting=meeting, old_starts_at=old, new_starts_at=new_starts_at, reason=reason,
        changed_by=user if user is not None and user.is_authenticated else None,
    )
    if meeting.original_starts_at is None:
        meeting.original_starts_at = old
    meeting.starts_at = new_starts_at
    meeting.status = Meeting.Status.POSTPONED
    meeting.reminder_3d_sent = False
    meeting.reminder_1d_sent = False
    meeting.save()
    body = f"Заседание перенесено с {local_str(old)} на {local_str(new_starts_at)}."
    if reason:
        body += f"\nПричина: {reason}"
    notify(
        meeting_recipients(meeting), Event.MEETING_SCHEDULED,
        f"Перенос заседания {meeting.commission.short_name}: {local_str(old)} → {local_str(new_starts_at)}",
        body + "\n" + _details(meeting), meeting_url(meeting), meeting.commission,
    )
    return meeting


@transaction.atomic
def cancel_meeting(meeting, reason, user=None):
    if not meeting.is_upcoming:
        raise ValidationError("Отменить можно только запланированное заседание.")
    meeting.status = Meeting.Status.CANCELLED
    meeting.cancel_reason = reason
    meeting.save()
    for voting in meeting.votings.filter(status__in=["draft", "open"]):
        voting.status = "cancelled"
        voting.save(update_fields=["status"])
    notify(
        meeting_recipients(meeting), Event.MEETING_SCHEDULED,
        f"Отменено заседание {_title(meeting)}",
        f"Заседание отменено.\nПричина: {reason}\n" + _details(meeting),
        meeting_url(meeting), meeting.commission,
    )
    log("cancel", meeting, {"reason": reason})
    return meeting


def mark_held(meeting, user=None):
    if not meeting.is_upcoming:
        raise ValidationError("Отметить проведённым можно только запланированное заседание.")
    if meeting.meeting_date > timezone.localdate():
        raise ValidationError("Заседание ещё не наступило.")
    meeting.status = Meeting.Status.HELD
    meeting.save()
    log("held", meeting)
    return meeting


# ---------- повестка ----------

def renumber(meeting):
    """Пункт контроля исполнения — всегда первый, остальные по порядку."""
    items = sorted(meeting.agenda_items.all(), key=lambda i: (not i.is_control_item, i.position, i.id))
    for index, item in enumerate(items, start=1):
        if item.position != index:
            item.position = index
            item.save(update_fields=["position"])
    return items


def next_position(meeting):
    return (meeting.agenda_items.aggregate(m=Max("position"))["m"] or 0) + 1


def agenda_changed(meeting, user=None):
    """После изменения утверждённой (или направленной) повестки требуется повторное утверждение."""
    if meeting.agenda_status != Meeting.AgendaStatus.DRAFT:
        meeting.agenda_status = Meeting.AgendaStatus.DRAFT
        meeting.agenda_approved_at = None
        meeting.agenda_approved_by = None
        meeting.save(update_fields=["agenda_status", "agenda_approved_at", "agenda_approved_by"])
        return True
    return False


def move_item(item, direction):
    meeting = item.meeting
    items = renumber(meeting)
    if item.is_control_item:
        return False
    idx = next(i for i, it in enumerate(items) if it.pk == item.pk)
    target = idx - 1 if direction == "up" else idx + 1
    if target < 0 or target >= len(items) or items[target].is_control_item:
        return False
    current, other = items[idx], items[target]
    current.position, other.position = other.position, current.position
    current.save(update_fields=["position"])
    other.save(update_fields=["position"])
    return True


def open_assignments(commission):
    from apps.protocols.models import Assignment, Protocol

    return (
        Assignment.objects.filter(commission=commission, status__in=Assignment.OPEN_STATUSES)
        .exclude(decision__protocol__status=Protocol.Status.ANNULLED)
        .select_related("responsible", "decision", "decision__protocol")
        .order_by("due_date", "id")
    )


def control_description(assignments):
    today = timezone.localdate()
    lines = [f"Открытых поручений: {len(assignments)} (по состоянию на {today:%d.%m.%Y})."]
    for n, a in enumerate(assignments, start=1):
        source = ""
        protocol = a.decision.protocol if a.decision_id else None
        if protocol is not None:
            source = f" [протокол № {protocol.number or 'б/н'} от {protocol.protocol_date:%d.%m.%Y}]"
        overdue = " — ПРОСРОЧЕНО" if a.is_overdue else ""
        responsible = a.responsible_display or "не назначен"
        lines.append(
            f"{n}. {a.text.strip()} — ответственный: {responsible}; срок: {a.due_date:%d.%m.%Y}{overdue}{source}"
        )
    return "\n".join(lines)


@transaction.atomic
def ensure_control_item(meeting, force=False):
    """Создаёт/обновляет первый вопрос «Контроль исполнения решений».

    Возвращает пункт повестки или None (если открытых поручений нет).
    Утверждённую повестку не меняет, если не указан force.
    """
    if not meeting.is_upcoming:
        return meeting.agenda_items.filter(is_control_item=True).first()
    if meeting.agenda_status == Meeting.AgendaStatus.APPROVED and not force:
        return meeting.agenda_items.filter(is_control_item=True).first()
    assignments = list(open_assignments(meeting.commission))
    item = meeting.agenda_items.filter(is_control_item=True).first()
    if not assignments:
        if item is not None and not item.votings.exists():
            item.delete()
            renumber(meeting)
        return None
    description = control_description(assignments)
    if item is None:
        secretary = secretary_for(meeting)
        item = AgendaItem(
            meeting=meeting, position=0, title=CONTROL_ITEM_TITLE, is_control_item=True,
            speaker=secretary, duration_minutes=10,
        )
    if item.description != description or item.pk is None:
        item.description = description
        item.save()
        if force:
            agenda_changed(meeting)
    renumber(meeting)
    return item


@transaction.atomic
def submit_agenda(meeting, user):
    if meeting.agenda_status != Meeting.AgendaStatus.DRAFT:
        raise ValidationError("Повестка уже направлена или утверждена.")
    if not meeting.agenda_items.exists():
        raise ValidationError("Повестка пуста.")
    meeting.agenda_status = Meeting.AgendaStatus.SUBMITTED
    meeting.save(update_fields=["agenda_status"])
    notify(
        chairman_for(meeting), Event.AGENDA_APPROVAL,
        f"Повестка на утверждение: {_title(meeting)}",
        "Секретарь направил проект повестки заседания на утверждение.",
        meeting_url(meeting), meeting.commission,
    )
    return meeting


@transaction.atomic
def approve_agenda(meeting, user):
    if not can_approve_agenda(user, meeting):
        raise PermissionDenied("Утвердить повестку может только председатель комиссии.")
    if meeting.agenda_status == Meeting.AgendaStatus.APPROVED:
        raise ValidationError("Повестка уже утверждена.")
    if not meeting.agenda_items.exists():
        raise ValidationError("Повестка пуста.")
    meeting.agenda_status = Meeting.AgendaStatus.APPROVED
    meeting.agenda_approved_at = timezone.now()
    meeting.agenda_approved_by = user
    meeting.save(update_fields=["agenda_status", "agenda_approved_at", "agenda_approved_by"])
    # Предыдущие отметки относятся к прежней редакции повестки.
    meeting.acknowledgements.all().delete()
    notify(
        meeting_recipients(meeting), Event.AGENDA_APPROVAL,
        f"Утверждена повестка: {_title(meeting)}",
        "Председатель утвердил повестку заседания. Пожалуйста, ознакомьтесь с повесткой и материалами "
        "и отметьте «Ознакомлен» либо направьте предложение.\n" + _details(meeting),
        meeting_url(meeting), meeting.commission,
    )
    return meeting


def return_agenda(meeting, user, comment=""):
    if not can_approve_agenda(user, meeting):
        raise PermissionDenied("Вернуть повестку может только председатель комиссии.")
    agenda_changed(meeting)
    notify(
        secretary_for(meeting), Event.AGENDA_APPROVAL,
        f"Повестка возвращена на доработку: {_title(meeting)}",
        comment, meeting_url(meeting), meeting.commission,
    )


def acknowledge(meeting, user, status, text=""):
    if not can_acknowledge(user, meeting):
        raise PermissionDenied("Ознакомление недоступно.")
    ack, _ = AgendaAcknowledgement.objects.update_or_create(
        meeting=meeting, user=user, defaults={"status": status, "text": text if status != "ack" else ""},
    )
    if status == AgendaAcknowledgement.Status.SUGGESTION:
        notify(
            secretary_for(meeting), Event.AGENDA_PROPOSAL,
            f"Предложение по повестке: {_title(meeting)}",
            f"{user.short_name} направил(а) предложение по утверждённой повестке.",
            meeting_url(meeting, "participants"), meeting.commission,
        )
    return ack


# ---------- предложения в повестку ----------

@transaction.atomic
def create_proposal(proposal, user):
    meeting = proposal.meeting
    if not can_propose(user, meeting):
        raise PermissionDenied("Приём предложений в повестку закрыт или вы не член комиссии.")
    proposal.author = user
    if proposal.file and not proposal.file_name:
        proposal.file_name = os.path.basename(proposal.file.name)
    proposal.save()
    notify(
        secretary_for(meeting), Event.AGENDA_PROPOSAL,
        f"Новое предложение в повестку: {_title(meeting)}",
        f"{user.short_name} предложил(а) вопрос в повестку заседания.",
        meeting_url(meeting, "proposals"), meeting.commission,
    )
    return proposal


def _copy_file(field, name):
    field.open("rb")
    try:
        return ContentFile(field.read(), name=name)
    finally:
        field.close()


@transaction.atomic
def accept_proposal(proposal, user):
    if proposal.status != AgendaProposal.Status.NEW:
        raise ValidationError("Предложение уже рассмотрено.")
    meeting = proposal.meeting
    patient_id = proposal.patient_id if meeting.commission.handles_patient_cases else ""
    item = AgendaItem.objects.create(
        meeting=meeting, position=next_position(meeting), title=proposal.title,
        description=proposal.description, speaker_name=proposal.speaker_name,
        patient_id=patient_id, requires_vote=proposal.requires_vote, proposal=proposal,
    )
    if proposal.file:
        name = proposal.file_name or os.path.basename(proposal.file.name)
        content = _copy_file(proposal.file, name)
        Material.objects.create(
            meeting=meeting, agenda_item=item, kind=Material.Kind.FILE, title=name,
            file=content, file_name=name, file_size=content.size, uploaded_by=proposal.author,
        )
    proposal.status = AgendaProposal.Status.ACCEPTED
    proposal.reviewed_by = user
    proposal.save()
    renumber(meeting)
    agenda_changed(meeting)
    notify(
        proposal.author, Event.AGENDA_PROPOSAL,
        f"Ваше предложение включено в повестку: {_title(meeting)}",
        "Секретарь включил предложенный вами вопрос в повестку заседания.",
        meeting_url(meeting), meeting.commission,
    )
    return item


@transaction.atomic
def reject_proposal(proposal, user, response):
    if proposal.status != AgendaProposal.Status.NEW:
        raise ValidationError("Предложение уже рассмотрено.")
    proposal.status = AgendaProposal.Status.REJECTED
    proposal.response = response
    proposal.reviewed_by = user
    proposal.save()
    meeting = proposal.meeting
    notify(
        proposal.author, Event.AGENDA_PROPOSAL,
        f"Предложение в повестку отклонено: {_title(meeting)}",
        f"Ответ секретаря: {response}",
        meeting_url(meeting, "proposals"), meeting.commission,
    )
    return proposal


# ---------- материалы ----------

def add_material(material, user, send_notifications=True):
    if material.file:
        material.kind = Material.Kind.FILE
        if not material.file_name:
            material.file_name = os.path.basename(material.file.name)
        material.file_size = material.file.size
    material.uploaded_by = user
    material.save()
    if send_notifications:
        meeting = material.meeting
        what = {
            Material.Kind.AUDIO: "аудиозапись", Material.Kind.VIDEO: "видеозапись",
        }.get(material.kind, "материалы")
        notify(
            meeting_recipients(meeting), Event.MATERIALS_ADDED,
            f"Добавлены {what}: {_title(meeting)}",
            f"К заседанию добавлены {what}.",
            meeting_url(meeting, "records" if material.kind == Material.Kind.AUDIO else "materials"),
            meeting.commission,
        )
    return material


def delete_material(material):
    if material.file:
        material.file.delete(save=False)
    material.delete()


# ---------- участники ----------

def attendance_rows(meeting):
    """Строки формы присутствия: состав на дату заседания + приглашённые."""
    existing = {a.user_id: a for a in meeting.attendance.select_related("reason")}
    rows, seen = [], set()
    for rec in meeting.composition():
        if rec.user_id in seen:
            continue
        seen.add(rec.user_id)
        rows.append({"user": rec.user, "role": rec.get_role_display(), "invited": False, "record": existing.get(rec.user_id)})
    for inv in meeting.invitations.select_related("user"):
        if inv.user_id in seen:
            continue
        seen.add(inv.user_id)
        rows.append({"user": inv.user, "role": "Приглашённый", "invited": True, "record": existing.get(inv.user_id)})
    return rows


@transaction.atomic
def save_attendance(meeting, values):
    """values: {user_id: (status or '', reason or None)}; пустой статус — снять отметку."""
    rows = {r["user"].pk: r for r in attendance_rows(meeting)}
    for user_id, (status, reason) in values.items():
        row = rows.get(user_id)
        if row is None:
            continue
        if not status:
            Attendance.objects.filter(meeting=meeting, user_id=user_id).delete()
            continue
        if status not in Attendance.Status.values:
            raise ValidationError("Недопустимая отметка присутствия.")
        if status in (Attendance.Status.PRESENT, Attendance.Status.ONLINE):
            reason = None
        Attendance.objects.update_or_create(
            meeting=meeting, user_id=user_id,
            defaults={"status": status, "reason": reason, "is_invited": row["invited"]},
        )
    return meeting.quorum()


def add_invitation(invitation, user):
    invitation.created_by = user
    invitation.save()
    meeting = invitation.meeting
    notify(
        invitation.user, Event.MEETING_SCHEDULED,
        f"Приглашение на заседание {_title(meeting)}",
        "Вы приглашены для участия в заседании. Доступ к повестке и материалам открыт "
        f"до {invitation.valid_until:%d.%m.%Y}.\n" + _details(meeting),
        meeting_url(meeting), meeting.commission,
    )
    return invitation


def ack_table(meeting):
    acks = {a.user_id: a for a in meeting.acknowledgements.all()}
    return [{"user": u, "ack": acks.get(u.pk)} for u in meeting_recipients(meeting)]


def visible_meetings(user):
    """Заседания видимых комиссий + заседания, куда пользователь приглашён."""
    from django.db.models import Q

    ids = list(commission_access.visible_commissions(user).values_list("pk", flat=True))
    invited = list(
        Invitation.objects.filter(user=user, valid_until__gte=timezone.localdate()).values_list("meeting_id", flat=True)
    ) if user.is_authenticated else []
    return Meeting.objects.filter(Q(commission_id__in=ids) | Q(pk__in=invited)).select_related("commission")
