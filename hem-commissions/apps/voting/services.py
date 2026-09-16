"""Бизнес-логика электронного голосования.

Правила (решение заказчика):
* голосуют члены комиссии, входящие в состав на дату открытия голосования
  (председатель, заместитель, секретарь, члены) — их список фиксируется;
* кворум — не менее 2/3 членов комиссии (Commission.vote_quorum_needed);
* решение принято, если кворум набран и «за» более половины проголосовавших.
"""
from datetime import datetime, time

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.urls import reverse
from django.utils import timezone

from apps.audit import services as audit
from apps.committees import access
from apps.committees.workdays import add_workdays
from apps.notifications.models import Event
from apps.notifications.services import notify

from .models import Vote, VotingSession

DEFAULT_DEADLINE_TIME = time(18, 0)


def default_deadline(commission, today=None):
    """Срок по умолчанию: через vote_days рабочих дней, 18:00 местного времени."""
    today = today or timezone.localdate()
    day = add_workdays(today, commission.vote_days)
    return timezone.make_aware(datetime.combine(day, DEFAULT_DEADLINE_TIME))


def session_url(session):
    return reverse("voting:detail", args=[session.pk])


def compute_outcome(eligible_total, quorum_needed, votes_for, votes_against, votes_abstain):
    cast = votes_for + votes_against + votes_abstain
    if eligible_total == 0 or cast < quorum_needed:
        return VotingSession.Outcome.NO_QUORUM
    if votes_for * 2 > cast:
        return VotingSession.Outcome.ADOPTED
    return VotingSession.Outcome.REJECTED


def outcome_summary(session):
    """Строка итогов: «за — 5, против — 1, воздержались — 0»."""
    return (
        f"за — {session.votes_for}, против — {session.votes_against}, "
        f"воздержались — {session.votes_abstain}"
    )


def progress(session):
    """Ход голосования: сколько проголосовало из имеющих право, сколько нужно для кворума."""
    eligible_ids = set(session.eligible.values_list("pk", flat=True))
    if session.status == VotingSession.Status.DRAFT:
        total = session.commission.member_users_on(timezone.localdate()).filter(is_active=True).count()
        needed = session.commission.vote_quorum_needed(total)
        voted = 0
    else:
        total = len(eligible_ids)
        needed = session.quorum_needed or session.commission.vote_quorum_needed(total)
        voted = session.votes.filter(user_id__in=eligible_ids).count()
    percent = round(voted * 100 / total) if total else 0
    return {
        "total": total, "voted": voted, "needed": needed, "reached": total > 0 and voted >= needed,
        "percent": percent, "needed_percent": round(needed * 100 / total) if total else 0,
    }


@transaction.atomic
def open_session(session, user):
    if session.status != VotingSession.Status.DRAFT:
        raise ValidationError("Открыть можно только голосование в статусе «Подготовка».")
    now = timezone.now()
    if session.deadline <= now:
        raise ValidationError("Срок окончания голосования уже прошёл — измените срок.")
    today = timezone.localdate()
    members = list(session.commission.member_users_on(today).filter(is_active=True))
    if not members:
        raise ValidationError("В действующем составе комиссии нет членов — голосование открыть нельзя.")
    session.eligible.set(members)
    session.quorum_needed = session.commission.vote_quorum_needed(len(members))
    session.status = VotingSession.Status.OPEN
    session.opened_at = now
    session.save()
    audit.log("voting_open", session, {"eligible": [u.pk for u in members], "quorum_needed": session.quorum_needed}, user=user)
    deadline = timezone.localtime(session.deadline)
    kind = "заочное голосование" if session.is_absentee else "голосование по вопросу повестки заседания"
    body = (
        f"Комиссия: {session.commission.name}.\n"
        f"Открыто {kind}: «{session.question}».\n"
        f"Проголосуйте до {deadline:%d.%m.%Y %H:%M}. "
        f"Кворум — {session.commission.vote_quorum_label}."
    )
    notify(
        members, Event.VOTING, f"Голосование: {session.question}", body, session_url(session),
        commission=session.commission, email_subject=f"Голосование: {session.question}"[:250],
    )
    return session


@transaction.atomic
def cast_vote(session, user, choice, comment="", entered_by=None):
    """Принимает голос. entered_by — секретарь, вносящий голос со слов члена комиссии."""
    session = VotingSession.objects.select_for_update().select_related("commission").get(pk=session.pk)
    if choice not in Vote.Choice.values:
        raise ValidationError("Выберите вариант ответа.")
    if not session.eligible.filter(pk=user.pk).exists():
        raise PermissionDenied("Вы не входите в число голосующих по этому вопросу.")
    if entered_by is not None and not access.is_secretary(entered_by, session.commission):
        raise PermissionDenied("Вносить голоса за членов комиссии может только секретарь.")
    if not session.is_open:
        raise ValidationError("Голосование закрыто.")
    existing = Vote.objects.filter(session=session, user=user).first()
    if existing and not session.commission.allow_vote_change:
        raise ValidationError("Голос уже принят. Изменение голоса в этой комиссии не допускается.")
    if not session.allow_comments:
        comment = ""
    if existing:
        existing.choice = choice
        existing.comment = comment
        existing.entered_by = entered_by
        existing.save()
        vote = existing
    else:
        vote = Vote.objects.create(session=session, user=user, choice=choice, comment=comment, entered_by=entered_by)
    audit.log(
        "vote_change" if existing else "vote",
        session,
        {"voter": user.pk, "entered_by": entered_by.pk if entered_by else None},
        user=entered_by or user,
    )
    maybe_close_if_complete(session)
    return vote


def maybe_close_if_complete(session):
    """Закрывает голосование досрочно, если проголосовали все имеющие право."""
    if session.status != VotingSession.Status.OPEN:
        return False
    eligible_ids = set(session.eligible.values_list("pk", flat=True))
    if not eligible_ids:
        return False
    voted = session.votes.filter(user_id__in=eligible_ids).count()
    if voted >= len(eligible_ids):
        close_session(session)
        return True
    return False


@transaction.atomic
def close_session(session, now=None, user=None):
    now = now or timezone.now()
    session = VotingSession.objects.select_for_update().select_related("commission").get(pk=session.pk)
    if session.status != VotingSession.Status.OPEN:
        return session
    eligible = list(session.eligible.all())
    eligible_ids = {u.pk for u in eligible}
    votes = session.votes.filter(user_id__in=eligible_ids)
    session.votes_for = votes.filter(choice=Vote.Choice.FOR).count()
    session.votes_against = votes.filter(choice=Vote.Choice.AGAINST).count()
    session.votes_abstain = votes.filter(choice=Vote.Choice.ABSTAIN).count()
    session.quorum_needed = session.commission.vote_quorum_needed(len(eligible))
    session.outcome = compute_outcome(
        len(eligible), session.quorum_needed, session.votes_for, session.votes_against, session.votes_abstain
    )
    session.status = VotingSession.Status.CLOSED
    session.closed_at = now
    session.save()
    audit.log("voting_close", session, {"outcome": session.outcome, "manual": user is not None}, user=user)
    body = (
        f"Комиссия: {session.commission.name}.\n"
        f"Голосование «{session.question}» завершено.\n"
        f"Итог: {session.get_outcome_display()}.\n"
        f"Проголосовали {session.votes_for + session.votes_against + session.votes_abstain} из {len(eligible)} "
        f"(для кворума нужно {session.quorum_needed}): {outcome_summary(session)}."
    )
    notify(
        eligible, Event.VOTING, f"Итоги голосования: {session.question}", body, session_url(session),
        commission=session.commission,
    )
    return session


@transaction.atomic
def cancel_session(session, user, reason=""):
    if session.status not in (VotingSession.Status.DRAFT, VotingSession.Status.OPEN):
        raise ValidationError("Отменить можно только готовящееся или идущее голосование.")
    was_open = session.status == VotingSession.Status.OPEN
    session.status = VotingSession.Status.CANCELLED
    session.closed_at = timezone.now()
    session.save()
    audit.log("voting_cancel", session, {"reason": reason}, user=user)
    if was_open:
        body = f"Комиссия: {session.commission.name}.\nГолосование «{session.question}» отменено."
        if reason:
            body += f"\nПричина: {reason}"
        notify(
            list(session.eligible.all()), Event.VOTING, f"Голосование отменено: {session.question}", body,
            session_url(session), commission=session.commission,
        )
    return session


def results_visible_to(session, user):
    """Что пользователь видит из результатов.

    Возвращает {"totals": bool, "per_person": bool, "progress_names": bool}.
    Руководители комиссии (секретарь, председатель, администратор) видят всё всегда.
    Члены и наблюдатели — итоги после завершения; поимённо — только при открытом
    голосовании в настройках комиссии.
    """
    commission = session.commission
    if access.can_manage(user, commission):
        return {"totals": True, "per_person": True, "progress_names": True}
    viewer = access.can_view(user, commission) or session.eligible.filter(pk=user.pk).exists()
    closed = session.status == VotingSession.Status.CLOSED
    return {
        "totals": viewer and closed,
        "per_person": viewer and closed and bool(commission.open_voting),
        "progress_names": False,
    }


def can_view_session(user, session):
    if access.can_view(user, session.commission):
        return True
    return session.eligible.filter(pk=user.pk).exists()
