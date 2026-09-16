"""Бизнес-логика протоколов: черновик, согласование, особые мнения, подпись,
аннулирование, поручения."""
import hashlib
import re
from datetime import timedelta

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile
from django.db import transaction
from django.urls import reverse
from django.utils import timezone

from apps.audit.services import log
from apps.committees import access
from apps.committees.models import CommissionAccess, MemberRecord
from apps.committees.services import check_signing_code, send_signing_code, sha256_bytes, sha256_field
from apps.committees.workdays import add_workdays
from apps.notifications.models import Event
from apps.notifications.services import notify

from . import names, pdf
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

FORMAT_TEXT = {
    "offline": "очное заседание",
    "online": "заседание в режиме видеоконференции",
    "mixed": "смешанное заседание (очно и в режиме видеоконференции)",
}
ABSENTEE_FORMAT = "заочное голосование"
TRANSCRIPT_NOTE = "[Полный текст транскрипта — распределите выступления по вопросам повестки]"


def protocol_url(protocol):
    return reverse("protocols:detail", args=[protocol.pk])


def assignment_url(assignment):
    return reverse("protocols:assignment_detail", args=[assignment.pk])


def _title(protocol):
    return f"{protocol.commission.short_name}: протокол № {protocol.number or 'б/н'} от {protocol.protocol_date:%d.%m.%Y}"


# ---------------------------------------------------------------- номер, итоги


def next_number(commission, year):
    seq = Protocol.objects.filter(commission=commission, protocol_date__year=year).count() + 1
    while True:
        number = f"{commission.short_name}-{year}/{seq:02d}"
        if not Protocol.objects.filter(commission=commission, number=number).exists():
            return number
        seq += 1


def vote_summary(session):
    """«За — X, против — Y, воздержались — Z; кворум …; решение принято/не принято»."""
    voted = session.votes_for + session.votes_against + session.votes_abstain
    eligible = session.eligible.count()
    if session.outcome == session.Outcome.NO_QUORUM:
        quorum = f"кворум отсутствует (проголосовали {voted} из {eligible}, требуется {session.quorum_needed})"
        result = "решение не принято"
    else:
        quorum = f"кворум имеется (проголосовали {voted} из {eligible}, требуется {session.quorum_needed})"
        result = "решение принято" if session.outcome == session.Outcome.ADOPTED else "решение не принято"
    text = (
        f"За — {session.votes_for}, против — {session.votes_against}, "
        f"воздержались — {session.votes_abstain}; {quorum}; {result}"
    )
    return text[:300]


def _closed_session_for(agenda_item):
    from apps.voting.models import VotingSession

    return (
        VotingSession.objects.filter(agenda_item=agenda_item, status=VotingSession.Status.CLOSED)
        .order_by("-closed_at", "-id")
        .first()
    )


def split_transcript(text, titles):
    """Делит транскрипт по названиям вопросов. None — если не удалось."""
    if not text or not titles:
        return None
    low = text.casefold()
    positions = []
    start = 0
    for title in titles:
        key = re.sub(r"\s+", " ", title).strip().casefold()
        if not key:
            return None
        idx = low.find(key, start)
        if idx < 0:
            return None
        positions.append(idx)
        start = idx + len(key)
    parts = []
    for i, pos in enumerate(positions):
        end = positions[i + 1] if i + 1 < len(positions) else len(text)
        parts.append(text[pos:end].strip())
    # всё, что до первого вопроса, — в первый раздел
    head = text[: positions[0]].strip()
    if head:
        parts[0] = head + "\n" + parts[0]
    return parts


def _known_names(meeting=None):
    extra = []
    if meeting is not None:
        extra = [a.speaker_name for a in meeting.agenda_items.all() if a.speaker_name]
    return names.known_names_from_system(extra)


# ---------------------------------------------------------------- создание


@transaction.atomic
def create_draft_from_meeting(meeting, user, transcript=None):
    commission = meeting.commission
    local = timezone.localtime(meeting.starts_at)
    protocol = Protocol.objects.create(
        commission=commission,
        kind=Protocol.Kind.MEETING,
        meeting=meeting,
        number=next_number(commission, local.year),
        protocol_date=local.date(),
        place=meeting.place or ("видеоконференция" if meeting.format == "online" else ""),
        format_text=FORMAT_TEXT.get(meeting.format, meeting.get_format_display()),
        preamble=f"Время начала заседания: {local:%H:%M}.",
        transcription_used=transcript is not None,
        created_by=user if user and user.pk else None,
    )
    agenda = list(meeting.agenda_items.select_related("speaker").order_by("position", "id"))
    parts = None
    if transcript is not None and transcript.text:
        parts = split_transcript(transcript.text, [a.title for a in agenda]) if len(agenda) > 1 else [transcript.text]
    known = _known_names(meeting)
    for n, agenda_item in enumerate(agenda):
        heard_lines = []
        if agenda_item.speaker_display:
            heard_lines.append(f"Докладчик: {agenda_item.speaker_display}")
        if agenda_item.description:
            heard_lines.append(agenda_item.description)
        discussed = ""
        if transcript is not None and transcript.text:
            if parts is not None:
                discussed = parts[n]
            elif n == 0:
                discussed = f"{TRANSCRIPT_NOTE}\n{transcript.text}"
        item = ProtocolItem(
            protocol=protocol,
            agenda_item=agenda_item,
            position=n + 1,
            title=agenda_item.title,
            patient_id=agenda_item.patient_id,
            heard="\n".join(heard_lines),
            discussed=discussed,
        )
        session = _closed_session_for(agenda_item)
        if session is not None:
            item.voting = session
            item.vote_summary = vote_summary(session)
            protocol.votings.add(session)
        names.flag_item(item, known)
        item.save()
    log("protocol_draft_created", protocol, {"meeting": meeting.pk, "transcript": transcript.pk if transcript else None})
    return protocol


@transaction.atomic
def create_absentee_protocol(sessions, user):
    from apps.voting.models import VotingSession

    sessions = list(sessions)
    if not sessions:
        raise ValidationError("Не выбрано ни одного голосования.")
    commission = sessions[0].commission
    for s in sessions:
        if s.commission_id != commission.pk:
            raise ValidationError("Все голосования должны относиться к одной комиссии.")
        if s.status != VotingSession.Status.CLOSED:
            raise ValidationError(f"Голосование «{s.question}» ещё не завершено.")
    today = timezone.localdate()
    protocol = Protocol.objects.create(
        commission=commission,
        kind=Protocol.Kind.ABSENTEE,
        number=next_number(commission, today.year),
        protocol_date=today,
        format_text=ABSENTEE_FORMAT,
        created_by=user if user and user.pk else None,
    )
    known = _known_names()
    for n, s in enumerate(sorted(sessions, key=lambda x: (x.closed_at or x.deadline, x.pk)), 1):
        item = ProtocolItem(
            protocol=protocol, voting=s, position=n, title=s.question[:500], heard=s.description,
            resolved=s.get_outcome_display() if s.outcome else "", vote_summary=vote_summary(s),
            agenda_item=s.agenda_item,
            patient_id=s.agenda_item.patient_id if s.agenda_item_id else "",
        )
        names.flag_item(item, known)
        item.save()
        protocol.votings.add(s)
    log("protocol_absentee_created", protocol, {"sessions": [s.pk for s in sessions]})
    return protocol


def renumber_items(protocol):
    for n, item in enumerate(protocol.items.order_by("position", "id"), 1):
        if item.position != n:
            item.position = n
            item.save(update_fields=["position"])


def refresh_flags(item):
    known = _known_names(item.protocol.meeting)
    names.flag_item(item, known)


def has_flagged_names(protocol):
    return any(item.flagged_names for item in protocol.items.all())


def requires_deidentification(protocol):
    return protocol.commission.handles_patient_cases and has_flagged_names(protocol)


@transaction.atomic
def create_replacement(protocol, user):
    """Новый протокол-черновик взамен аннулированного (копия содержания)."""
    if protocol.status != Protocol.Status.ANNULLED:
        raise ValidationError("Замена создаётся только для аннулированного протокола.")
    new = Protocol.objects.create(
        commission=protocol.commission, kind=protocol.kind, meeting=protocol.meeting,
        number=next_number(protocol.commission, timezone.localdate().year),
        protocol_date=protocol.protocol_date, place=protocol.place, format_text=protocol.format_text,
        preamble=protocol.preamble, transcription_used=protocol.transcription_used,
        replaces=protocol, created_by=user,
    )
    new.votings.set(protocol.votings.all())
    for item in protocol.items.order_by("position", "id"):
        new_item = ProtocolItem.objects.create(
            protocol=new, agenda_item=item.agenda_item, voting=item.voting, position=item.position,
            title=item.title, patient_id=item.patient_id, heard=item.heard, discussed=item.discussed,
            resolved=item.resolved, vote_summary=item.vote_summary, flagged_names=item.flagged_names,
        )
        for d in item.decisions.all():
            new_d = Decision.objects.create(protocol=new, item=new_item, number=d.number, text=d.text)
            for a in d.assignments.all():
                new_a = Assignment.objects.create(
                    decision=new_d, text=a.text, responsible=a.responsible,
                    responsible_name=a.responsible_name, due_date=a.due_date,
                )
                new_a.co_executors.set(a.co_executors.all())
    log("protocol_replacement_created", new, {"replaces": protocol.pk})
    return new


# ---------------------------------------------------------------- снимок


def _person(user, role="", position=""):
    return {
        "name": user.full_name or user.username,
        "role": role,
        "position": position or (user.position.name if user.position_id else ""),
    }


def _attendance_snapshot(protocol):
    from apps.meetings.models import Attendance

    commission = protocol.commission
    records = list(commission.members_on(protocol.protocol_date))
    present, absent, invited = [], [], []
    if protocol.kind == Protocol.Kind.ABSENTEE:
        voted = set()
        for s in protocol.votings.all():
            voted.update(s.votes.values_list("user_id", flat=True))
        for rec in records:
            entry = _person(rec.user, rec.get_role_display(), rec.position_text)
            (present if rec.user_id in voted else absent).append(entry)
        return {"present": present, "absent": absent, "invited": []}, None

    meeting = protocol.meeting
    if meeting is None:
        return {"present": [], "absent": [], "invited": []}, None
    marks = {a.user_id: a for a in meeting.attendance.select_related("user", "user__position", "reason")}
    member_ids = set()
    for rec in records:
        member_ids.add(rec.user_id)
        entry = _person(rec.user, rec.get_role_display(), rec.position_text)
        mark = marks.get(rec.user_id)
        if mark and mark.status in (Attendance.Status.PRESENT, Attendance.Status.ONLINE):
            if mark.status == Attendance.Status.ONLINE:
                entry["role"] = f"{entry['role']} (онлайн)"
            present.append(entry)
        else:
            if mark and mark.reason_id:
                entry["reason"] = mark.reason.name
            elif mark:
                entry["reason"] = mark.get_status_display().lower()
            absent.append(entry)
    seen = set()
    for mark in marks.values():
        if mark.user_id in member_ids:
            continue
        if mark.is_invited and mark.status in (Attendance.Status.PRESENT, Attendance.Status.ONLINE):
            invited.append(_person(mark.user))
            seen.add(mark.user_id)
    q = meeting.quorum()
    q["label"] = commission.meeting_quorum_label
    return {"present": present, "absent": absent, "invited": invited}, q


def active_dissents(protocol):
    return protocol.dissents.filter(withdrawn_at__isnull=True, signed_at__isnull=False).select_related("author", "item")


def dissent_note(authors):
    if not authors:
        return ""
    if len(authors) == 1:
        return f"С особым мнением члена комиссии {authors[0]}."
    return f"С особым мнением членов комиссии: {', '.join(authors)}."


def build_snapshot(protocol, draft=False):
    commission = protocol.commission
    attendance, quorum = _attendance_snapshot(protocol)
    dissents = list(active_dissents(protocol))
    items = []
    for item in protocol.items.order_by("position", "id").prefetch_related(
        "decisions__assignments__co_executors", "decisions__assignments__responsible"
    ):
        authors = []
        for d in dissents:
            if d.item_id == item.pk:
                name = d.author.full_name or d.author.username
                if name not in authors:
                    authors.append(name)
        decisions = []
        for dec in item.decisions.order_by("id"):
            decisions.append({
                "number": dec.number,
                "text": dec.text,
                "assignments": [
                    {
                        "text": a.text,
                        "responsible": a.responsible.full_name if a.responsible_id else a.responsible_name,
                        "co_executors": [u.full_name or u.username for u in a.co_executors.order_by("last_name", "pk")],
                        "due_date": a.due_date.isoformat(),
                    }
                    for a in dec.assignments.order_by("due_date", "id")
                ],
            })
        items.append({
            "position": item.position,
            "title": item.title,
            "patient_id": item.patient_id,
            "heard": item.heard,
            "discussed": item.discussed,
            "resolved": item.resolved,
            "vote_summary": item.vote_summary,
            "voting_id": item.voting_id,
            "decisions": decisions,
            "dissenters": authors,
            "dissent_note": dissent_note(authors),
        })
    time_text = ""
    if protocol.meeting_id:
        time_text = f"{timezone.localtime(protocol.meeting.starts_at):%H:%M}"
    chairman = commission.officer(MemberRecord.Role.CHAIRMAN, protocol.protocol_date)
    secretary = commission.officer(MemberRecord.Role.SECRETARY, protocol.protocol_date)
    return {
        "version": 1,
        "draft": draft,
        "uid": str(protocol.uid),
        "organization": settings.ORGANIZATION_NAME,
        "commission": {"id": commission.pk, "name": commission.name, "short_name": commission.short_name},
        "kind": protocol.kind,
        "kind_display": protocol.get_kind_display(),
        "number": protocol.number,
        "date": protocol.protocol_date.isoformat(),
        "time": time_text,
        "place": protocol.place,
        "format": protocol.format_text,
        "preamble": protocol.preamble,
        "revision": protocol.revision,
        "meeting_id": protocol.meeting_id,
        "attendance": attendance,
        "quorum": quorum,
        "items": items,
        "dissents": [
            {
                "item_position": d.item.position,
                "item_title": d.item.title,
                "author": d.author.full_name or d.author.username,
                "text": d.text,
                "file_name": d.file_name,
                "signed_at": timezone.localtime(d.signed_at).replace(tzinfo=None).isoformat(timespec="seconds"),
                "text_hash": d.text_hash,
            }
            for d in sorted(dissents, key=lambda x: (x.item.position, x.created_at, x.pk))
        ],
        "transcription_used": protocol.transcription_used,
        "officers": {
            "chairman": chairman.full_name if chairman else "",
            "secretary": secretary.full_name if secretary else "",
        },
    }


def draft_pdf(protocol):
    return pdf.render_protocol(build_snapshot(protocol, draft=True))


# ---------------------------------------------------------------- согласование


def current_approvals(protocol):
    return protocol.approvals.filter(revision=protocol.revision).select_related("user", "item")


def pending_approval_for(protocol, user):
    if protocol.status != Protocol.Status.APPROVAL or not user.is_authenticated:
        return None
    return current_approvals(protocol).filter(user=user).first()


def approval_open(protocol, today=None):
    today = today or timezone.localdate()
    return (
        protocol.status == Protocol.Status.APPROVAL
        and protocol.approval_deadline is not None
        and today <= protocol.approval_deadline
    )


def check_ready_for_approval(protocol):
    if protocol.status != Protocol.Status.DRAFT:
        raise ValidationError("Отправить на согласование можно только черновик.")
    if not protocol.items.exists():
        raise ValidationError("В протоколе нет ни одного вопроса.")
    if requires_deidentification(protocol) and not protocol.patient_ids_confirmed:
        raise ValidationError(
            "В тексте найдены возможные ФИО пациентов. Замените их на ID из МИС и отметьте "
            "«обезличивание подтверждено»."
        )


@transaction.atomic
def send_to_approval(protocol, user):
    check_ready_for_approval(protocol)
    if protocol.approvals.filter(revision=protocol.revision).exists():
        protocol.revision += 1
    today = timezone.localdate()
    recipients = [
        u for u in protocol.commission.member_users_on(protocol.protocol_date).filter(is_active=True)
        if u.pk != user.pk
    ]
    for u in recipients:
        ProtocolApproval.objects.get_or_create(protocol=protocol, revision=protocol.revision, user=u)
    protocol.status = Protocol.Status.APPROVAL
    protocol.approval_started_at = timezone.now()
    protocol.approval_deadline = add_workdays(today, protocol.commission.approval_days)
    protocol.approval_reminder_sent = False
    protocol.save()
    log("protocol_sent_to_approval", protocol, {"revision": protocol.revision, "deadline": protocol.approval_deadline.isoformat()})
    notify(
        recipients, Event.PROTOCOL_APPROVAL,
        f"Проект протокола на согласование — {protocol.commission.short_name}",
        (
            f"{_title(protocol)} (редакция {protocol.revision}) направлен вам на согласование.\n"
            f"Срок — до {protocol.approval_deadline:%d.%m.%Y} включительно. Ответьте «согласен» или направьте замечания; "
            "в этот же срок может быть подано особое мнение.\n"
            "Отсутствие ответа до окончания срока означает отсутствие замечаний."
        ),
        protocol_url(protocol), protocol.commission,
    )
    return protocol


@transaction.atomic
def respond_approval(protocol, user, status, remarks="", item=None):
    row = pending_approval_for(protocol, user)
    if row is None:
        raise ValidationError("Вы не участвуете в согласовании этого протокола.")
    if not approval_open(protocol):
        raise ValidationError("Срок согласования истёк.")
    if status not in (ProtocolApproval.Status.AGREED, ProtocolApproval.Status.REMARKS):
        raise ValidationError("Некорректный ответ.")
    if status == ProtocolApproval.Status.REMARKS and not (remarks or "").strip():
        raise ValidationError("Укажите текст замечаний.")
    if item is not None and item.protocol_id != protocol.pk:
        raise ValidationError("Вопрос не относится к протоколу.")
    row.status = status
    row.remarks = remarks.strip() if status == ProtocolApproval.Status.REMARKS else ""
    row.item = item if status == ProtocolApproval.Status.REMARKS else None
    row.responded_at = timezone.now()
    row.save()
    if status == ProtocolApproval.Status.REMARKS:
        secretary = protocol.commission.officer(MemberRecord.Role.SECRETARY, protocol.protocol_date)
        notify(
            secretary, Event.PROTOCOL_APPROVAL, f"Замечания к проекту протокола — {protocol.commission.short_name}",
            f"{user.short_name} направил(а) замечания к проекту: {_title(protocol)}.",
            protocol_url(protocol), protocol.commission,
        )
    return row


@transaction.atomic
def new_revision(protocol, user):
    """Возврат проекта в черновик для новой редакции (особые мнения сохраняются)."""
    if protocol.status != Protocol.Status.APPROVAL:
        raise ValidationError("Новую редакцию можно подготовить только в период согласования.")
    protocol.status = Protocol.Status.DRAFT
    protocol.save()
    log("protocol_new_revision", protocol, {"from_revision": protocol.revision})
    return protocol


def non_responders(protocol):
    return [a.user for a in current_approvals(protocol).filter(status=ProtocolApproval.Status.PENDING)]


def freeze(protocol):
    snap = build_snapshot(protocol)
    data = pdf.render_protocol(snap)
    if protocol.frozen_pdf:
        protocol.frozen_pdf.delete(save=False)
    protocol.snapshot = snap
    protocol.frozen_pdf.save(f"protocol-{protocol.uid}-r{protocol.revision}.pdf", ContentFile(data), save=False)
    protocol.frozen_hash = sha256_bytes(data)
    return data


@transaction.atomic
def move_to_signing(protocol, user=None, auto=False):
    if protocol.status != Protocol.Status.APPROVAL:
        raise ValidationError("Протокол не находится на согласовании.")
    waiting = non_responders(protocol)
    if not auto and waiting:
        raise ValidationError(
            "Досрочно передать на подпись можно только после ответа всех членов комиссии "
            f"(нет ответа: {', '.join(u.short_name for u in waiting)})."
        )
    freeze(protocol)
    protocol.status = Protocol.Status.SIGNING
    protocol.save()
    log("protocol_frozen", protocol, {"frozen_hash": protocol.frozen_hash, "auto": auto, "revision": protocol.revision})
    commission = protocol.commission
    secretary = commission.officer(MemberRecord.Role.SECRETARY, protocol.protocol_date)
    notify(
        secretary, Event.PROTOCOL_SIGNING, f"Протокол на подпись — {commission.short_name}",
        f"{_title(protocol)} сформирован для подписания. Первым подписывает секретарь, затем председатель.",
        protocol_url(protocol), commission,
    )
    if auto:
        chairman = commission.officer(MemberRecord.Role.CHAIRMAN, protocol.protocol_date)
        if waiting:
            listing = "\n".join(f"— {u.full_name or u.username}" for u in waiting)
            body = (
                f"Срок согласования {_title(protocol)} истёк. Протокол передан на подпись.\n"
                f"Не ответили (молчание считается отсутствием замечаний):\n{listing}"
            )
        else:
            body = f"Срок согласования {_title(protocol)} истёк, все члены комиссии ответили. Протокол передан на подпись."
        notify(chairman, Event.PROTOCOL_APPROVAL, f"Итоги согласования протокола — {commission.short_name}",
               body, protocol_url(protocol), commission)
    return protocol


# ---------------------------------------------------------------- особые мнения


def dissent_purpose(protocol):
    return f"protocol-dissent:{protocol.pk}"


def request_dissent_code(protocol, user):
    check_can_dissent(protocol, user)
    send_signing_code(user, dissent_purpose(protocol), f"особое мнение к протоколу {_title(protocol)}")


def check_can_dissent(protocol, user):
    if not approval_open(protocol):
        raise ValidationError("Особое мнение принимается только в период согласования проекта протокола.")
    if not access.is_voting_member(user, protocol.commission, protocol.protocol_date):
        raise ValidationError("Особое мнение может подать только член комиссии.")


@transaction.atomic
def add_dissent(protocol, user, item, text, file, code, ip=None):
    check_can_dissent(protocol, user)
    if item.protocol_id != protocol.pk:
        raise ValidationError("Вопрос не относится к протоколу.")
    text = (text or "").strip()
    if not text and not file:
        raise ValidationError("Укажите текст особого мнения или приложите файл.")
    check_signing_code(user, dissent_purpose(protocol), code)
    digest = hashlib.sha256(text.encode("utf-8"))
    dissent = DissentingOpinion(protocol=protocol, item=item, author=user, text=text, signed_ip=ip)
    if file:
        content = file.read()
        file.seek(0)
        digest.update(b"\n" + hashlib.sha256(content).hexdigest().encode())
        dissent.file_name = file.name[:255]
        dissent.file = file
    dissent.text_hash = digest.hexdigest()
    dissent.signed_at = timezone.now()
    dissent.save()
    log("dissent_signed", dissent, {"protocol": protocol.pk, "item": item.pk, "text_hash": dissent.text_hash}, user=user, ip=ip)
    secretary = protocol.commission.officer(MemberRecord.Role.SECRETARY, protocol.protocol_date)
    notify(
        secretary, Event.PROTOCOL_APPROVAL, f"Подано особое мнение — {protocol.commission.short_name}",
        f"{user.short_name} подал(а) особое мнение к проекту: {_title(protocol)}.",
        protocol_url(protocol), protocol.commission,
    )
    return dissent


@transaction.atomic
def withdraw_dissent(dissent, user):
    if dissent.author_id != user.pk:
        raise ValidationError("Отозвать особое мнение может только его автор.")
    if dissent.withdrawn_at:
        raise ValidationError("Особое мнение уже отозвано.")
    if not approval_open(dissent.protocol):
        raise ValidationError("Отозвать особое мнение можно только в период согласования.")
    dissent.withdrawn_at = timezone.now()
    dissent.save()
    log("dissent_withdrawn", dissent, {"protocol": dissent.protocol_id})
    return dissent


# ---------------------------------------------------------------- подпись


def sign_purpose(protocol):
    return f"protocol-sign:{protocol.pk}:{protocol.frozen_hash[:16]}"


def expected_signer(protocol):
    role = protocol.next_signer_role()
    if role is None:
        return None, None
    return role, protocol.commission.officer(role, protocol.protocol_date)


def can_sign(protocol, user):
    if protocol.status != Protocol.Status.SIGNING or not user.is_authenticated:
        return False
    _, officer = expected_signer(protocol)
    return officer is not None and officer.pk == user.pk


def _check_signer(protocol, user):
    if protocol.status != Protocol.Status.SIGNING:
        raise ValidationError("Протокол не находится на подписи.")
    role, officer = expected_signer(protocol)
    if officer is None or officer.pk != user.pk:
        label = MemberRecord.Role(role).label.lower() if role else "подписант"
        raise ValidationError(f"Сейчас протокол подписывает {label} комиссии на дату протокола.")
    return role


def request_sign_code(protocol, user):
    _check_signer(protocol, user)
    send_signing_code(user, sign_purpose(protocol), f"{_title(protocol)} (SHA-256 {protocol.frozen_hash[:12]}…)")


@transaction.atomic
def sign(protocol, user, code, ip=None):
    protocol = Protocol.objects.select_for_update().get(pk=protocol.pk)
    role = _check_signer(protocol, user)
    check_signing_code(user, sign_purpose(protocol), code)
    if not protocol.frozen_pdf or sha256_field(protocol.frozen_pdf) != protocol.frozen_hash:
        raise ValidationError("Контрольная сумма документа на подпись не совпадает. Обратитесь к администратору.")
    record = protocol.commission.members_on(protocol.protocol_date).filter(user=user, role=role).first()
    signature = Signature.objects.create(
        protocol=protocol, user=user, role=role, full_name=user.full_name or user.username,
        position_text=(record.position_text if record else "") or (user.position.name if user.position_id else ""),
        ip_address=ip, document_hash=protocol.frozen_hash,
    )
    log("protocol_signed_by", protocol, {"role": role, "hash": protocol.frozen_hash, "signature": signature.pk}, user=user, ip=ip)
    next_role, next_officer = expected_signer(protocol)
    if next_role is None:
        finalize(protocol)
    else:
        notify(
            next_officer, Event.PROTOCOL_SIGNING, f"Протокол на подпись — {protocol.commission.short_name}",
            f"{_title(protocol)} подписан секретарём и ожидает вашей подписи.",
            protocol_url(protocol), protocol.commission,
        )
    return signature


def stamp_data(protocol):
    return {
        "frozen_hash": protocol.frozen_hash,
        "verify_url": protocol.verify_url,
        "signatures": [
            {
                "full_name": s.full_name,
                "role": s.get_role_display(),
                "position": s.position_text,
                "signed_at": timezone.localtime(s.signed_at).replace(tzinfo=None).isoformat(timespec="seconds"),
            }
            for s in protocol.active_signatures()
        ],
    }


def finalize(protocol):
    data = pdf.render_protocol(protocol.snapshot, stamp=stamp_data(protocol))
    protocol.signed_pdf.save(f"protocol-{protocol.uid}-signed.pdf", ContentFile(data), save=False)
    protocol.signed_hash = sha256_bytes(data)
    protocol.status = Protocol.Status.SIGNED
    protocol.signed_at = timezone.now()
    protocol.save()
    log("protocol_signed", protocol, {"frozen_hash": protocol.frozen_hash, "signed_hash": protocol.signed_hash})
    commission = protocol.commission
    recipients = list(commission.member_users_on(protocol.protocol_date))
    recipients += [a.user for a in CommissionAccess.objects.filter(commission=commission).select_related("user")]
    notify(
        recipients, Event.PROTOCOL_SIGNED, f"Протокол подписан — {commission.short_name}",
        f"{_title(protocol)} подписан и доступен в системе.", protocol_url(protocol), commission,
    )
    for a in Assignment.objects.filter(decision__protocol=protocol).prefetch_related("co_executors"):
        notify_assignment(
            a, f"Новое поручение — {commission.short_name}",
            f"Вам назначено поручение по протоколу № {protocol.number}. Срок — {a.due_date:%d.%m.%Y}.",
        )
    return data


@transaction.atomic
def return_for_rework(protocol, user, reason):
    if protocol.status != Protocol.Status.SIGNING:
        raise ValidationError("Вернуть на доработку можно только протокол на подписи.")
    chairman = protocol.commission.officer(MemberRecord.Role.CHAIRMAN, protocol.protocol_date)
    if chairman is None or chairman.pk != user.pk:
        raise ValidationError("Вернуть протокол на доработку может только председатель комиссии.")
    reason = (reason or "").strip()
    if not reason:
        raise ValidationError("Укажите причину возврата.")
    now = timezone.now()
    protocol.signatures.filter(revoked_at__isnull=True).update(revoked_at=now, revoke_reason=reason[:300])
    if protocol.frozen_pdf:
        protocol.frozen_pdf.delete(save=False)
    protocol.frozen_hash = ""
    protocol.snapshot = {}
    protocol.status = Protocol.Status.DRAFT
    protocol.save()
    log("protocol_returned", protocol, {"reason": reason})
    secretary = protocol.commission.officer(MemberRecord.Role.SECRETARY, protocol.protocol_date)
    notify(
        secretary, Event.PROTOCOL_SIGNING, f"Протокол возвращён на доработку — {protocol.commission.short_name}",
        f"Председатель вернул {_title(protocol)} на доработку. Причина: {reason}",
        protocol_url(protocol), protocol.commission,
    )
    return protocol


def can_annul(protocol, user):
    roles = access.roles_in(user, protocol.commission)
    return protocol.status == Protocol.Status.SIGNED and bool(roles & {access.ADMIN, access.CHAIRMAN})


@transaction.atomic
def annul(protocol, user, reason):
    if protocol.status != Protocol.Status.SIGNED:
        raise ValidationError("Аннулировать можно только подписанный протокол.")
    if not can_annul(protocol, user):
        raise ValidationError("Аннулировать протокол может председатель комиссии или администратор.")
    reason = (reason or "").strip()
    if not reason:
        raise ValidationError("Укажите причину аннулирования.")
    protocol.status = Protocol.Status.ANNULLED
    protocol.annulled_reason = reason
    protocol.annulled_at = timezone.now()
    protocol.save()
    for a in Assignment.objects.filter(decision__protocol=protocol, status__in=Assignment.OPEN_STATUSES):
        a.status = Assignment.Status.CANCELLED
        a.cancel_reason = "Протокол аннулирован"
        a.save()
    log("protocol_annulled", protocol, {"reason": reason})
    commission = protocol.commission
    notify(
        commission.member_users_on(protocol.protocol_date), Event.PROTOCOL_SIGNED,
        f"Протокол аннулирован — {commission.short_name}",
        f"{_title(protocol)} аннулирован. Причина: {reason}", protocol_url(protocol), commission,
    )
    return protocol


# ---------------------------------------------------------------- поручения


def notify_assignment(assignment, title, body):
    users = [assignment.responsible] if assignment.responsible_id else []
    users += list(assignment.co_executors.all())
    return notify(users, Event.ASSIGNMENT, title, body, assignment_url(assignment), assignment.commission)


def is_executor(assignment, user):
    return user.is_authenticated and (
        assignment.responsible_id == user.pk or assignment.co_executors.filter(pk=user.pk).exists()
    )


def can_confirm(assignment, user):
    return access.can_manage(user, assignment.commission)


def _require_active(assignment):
    if assignment.decision.protocol.status != Protocol.Status.SIGNED:
        raise ValidationError("Поручение вступает в силу после подписания протокола.")


def _comment(assignment, user, text="", file=None, status_change=""):
    comment = AssignmentComment(assignment=assignment, author=user, text=text or "", status_change=status_change)
    if file:
        comment.file = file
        comment.file_name = file.name[:255]
    comment.save()
    return comment


@transaction.atomic
def start_assignment(assignment, user):
    _require_active(assignment)
    if not (is_executor(assignment, user) or can_confirm(assignment, user)):
        raise ValidationError("Нет прав на изменение поручения.")
    if assignment.status != Assignment.Status.ASSIGNED:
        raise ValidationError("Поручение уже в работе.")
    assignment.status = Assignment.Status.IN_PROGRESS
    assignment.save()
    _comment(assignment, user, status_change=Assignment.Status.IN_PROGRESS)
    return assignment


@transaction.atomic
def report_done(assignment, user, text, file=None, completed_on=None):
    _require_active(assignment)
    if not is_executor(assignment, user):
        raise ValidationError("Отметить исполнение может ответственный или соисполнитель.")
    if assignment.status not in (Assignment.Status.ASSIGNED, Assignment.Status.IN_PROGRESS):
        raise ValidationError("Поручение не находится в работе.")
    if not (text or "").strip() and not file:
        raise ValidationError("Опишите результат исполнения или приложите файл.")
    today = timezone.localdate()
    completed_on = completed_on or today
    if completed_on > today:
        raise ValidationError("Дата исполнения не может быть в будущем.")
    assignment.status = Assignment.Status.ON_CONFIRMATION
    assignment.reported_at = timezone.now()
    assignment.completed_on = completed_on
    assignment.save()
    _comment(assignment, user, text, file, Assignment.Status.ON_CONFIRMATION)
    protocol = assignment.decision.protocol
    secretary = assignment.commission.officer(MemberRecord.Role.SECRETARY)
    notify(
        secretary, Event.ASSIGNMENT, f"Поручение ждёт подтверждения — {assignment.commission.short_name}",
        f"{user.short_name} отметил(а) исполнение поручения по протоколу № {protocol.number}.",
        assignment_url(assignment), assignment.commission,
    )
    return assignment


@transaction.atomic
def confirm_assignment(assignment, user, text=""):
    if not can_confirm(assignment, user):
        raise ValidationError("Подтвердить исполнение может секретарь, председатель или администратор.")
    if assignment.status != Assignment.Status.ON_CONFIRMATION:
        raise ValidationError("Поручение не ожидает подтверждения.")
    completed = assignment.completed_on or timezone.localdate()
    assignment.completed_on = completed
    assignment.status = Assignment.Status.DONE_LATE if completed > assignment.due_date else Assignment.Status.DONE
    assignment.confirmed_by = user
    assignment.save()
    _comment(assignment, user, text, status_change=assignment.status)
    notify_assignment(
        assignment, f"Исполнение поручения подтверждено — {assignment.commission.short_name}",
        f"Статус поручения: {assignment.get_status_display()}.",
    )
    return assignment


@transaction.atomic
def return_assignment(assignment, user, text):
    if not can_confirm(assignment, user):
        raise ValidationError("Вернуть поручение может секретарь, председатель или администратор.")
    if assignment.status != Assignment.Status.ON_CONFIRMATION:
        raise ValidationError("Поручение не ожидает подтверждения.")
    if not (text or "").strip():
        raise ValidationError("Укажите, что необходимо доработать.")
    assignment.status = Assignment.Status.IN_PROGRESS
    assignment.completed_on = None
    assignment.reported_at = None
    assignment.save()
    _comment(assignment, user, text, status_change="returned")
    notify_assignment(
        assignment, f"Поручение возвращено в работу — {assignment.commission.short_name}",
        "Секретарь вернул поручение на доработку. Подробности — в карточке поручения.",
    )
    return assignment


@transaction.atomic
def cancel_assignment(assignment, user, reason):
    roles = access.roles_in(user, assignment.commission)
    if not roles & {access.ADMIN, access.SECRETARY, access.CHAIRMAN}:
        raise ValidationError("Снять поручение может секретарь или председатель.")
    if not assignment.is_open:
        raise ValidationError("Поручение уже закрыто.")
    reason = (reason or "").strip()
    if not reason:
        raise ValidationError("Укажите причину снятия.")
    assignment.status = Assignment.Status.CANCELLED
    assignment.cancel_reason = reason[:300]
    assignment.save()
    _comment(assignment, user, reason, status_change=Assignment.Status.CANCELLED)
    notify_assignment(
        assignment, f"Поручение снято с контроля — {assignment.commission.short_name}", f"Причина: {reason}",
    )
    return assignment


def add_comment(assignment, user, text, file=None):
    if not (text or "").strip() and not file:
        raise ValidationError("Пустой комментарий.")
    return _comment(assignment, user, text, file)


def overdue_since(assignment, today=None):
    today = today or timezone.localdate()
    return (today - assignment.due_date).days if assignment.is_open and assignment.due_date < today else 0


def reminder_window(due_date, days):
    return due_date - timedelta(days=days)
