"""Периодические задачи голосования (вызываются планировщиком)."""
import logging
from datetime import timedelta

from django.utils import timezone

from apps.notifications.models import Event
from apps.notifications.services import notify

from . import services
from .models import VotingSession

logger = logging.getLogger(__name__)

REMINDER_BEFORE = timedelta(hours=24)


def close_expired(now):
    closed = 0
    for session in VotingSession.objects.filter(status=VotingSession.Status.OPEN, deadline__lte=now):
        try:
            services.close_session(session, now=now)
            closed += 1
        except Exception:  # noqa: BLE001
            logger.exception("Не удалось закрыть голосование %s", session.pk)
    return closed


def send_reminders(now):
    reminded = 0
    qs = VotingSession.objects.filter(
        status=VotingSession.Status.OPEN, reminder_sent=False,
        deadline__gt=now, deadline__lte=now + REMINDER_BEFORE,
    ).select_related("commission")
    for session in qs:
        voted = session.votes.values_list("user_id", flat=True)
        pending = list(session.eligible.exclude(pk__in=list(voted)))
        if pending:
            deadline = timezone.localtime(session.deadline)
            notify(
                pending, Event.VOTING, f"Напоминание: голосование «{session.question}»",
                (
                    f"Комиссия: {session.commission.name}.\n"
                    f"Голосование завершится {deadline:%d.%m.%Y в %H:%M}. Вы ещё не проголосовали."
                ),
                services.session_url(session), commission=session.commission,
                email_subject=f"Голосование: {session.question}"[:250],
            )
            reminded += len(pending)
        session.reminder_sent = True
        session.save(update_fields=["reminder_sent"])
    return reminded


def run(now=None):
    now = now or timezone.now()
    return {"closed": close_expired(now), "reminded": send_reminders(now)}
