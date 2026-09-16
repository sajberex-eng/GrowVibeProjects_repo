"""Регламентные задания приложения «Комиссии» (вызываются планировщиком).

run(now)                     — ежедневно: напоминания о плановом пересмотре положений.
run_monthly_legal_check(now) — ежемесячно: проверка актуальности НПА на adilet.zan.kz.
"""
from datetime import timedelta

from django.urls import reverse
from django.utils import timezone

from .models import Regulation

REVIEW_REMINDER_DAYS = 30


def _today(now):
    now = now or timezone.now()
    return timezone.localtime(now).date() if timezone.is_aware(now) else now.date()


def send_regulation_review_reminders(now=None):
    from apps.notifications.models import Event
    from apps.notifications.services import notify

    today = _today(now)
    due = Regulation.objects.filter(
        expired_on__isnull=True,
        review_on__isnull=False,
        review_on__lte=today + timedelta(days=REVIEW_REMINDER_DAYS),
        review_reminder_sent=False,
        commission__is_active=True,
    ).select_related("commission")
    sent = 0
    for reg in due:
        commission = reg.commission
        recipients = [u for u in (commission.officer("secretary", today), commission.officer("chairman", today)) if u]
        overdue = reg.review_on < today
        title = (
            f"{'Просрочен' if overdue else 'Приближается'} плановый пересмотр положения — {commission.short_name}"
        )
        body = (
            f"{reg.title} (ред. {reg.version}, утв. {reg.approved_on:%d.%m.%Y}).\n"
            f"Дата планового пересмотра: {reg.review_on:%d.%m.%Y}.\n"
            "Подготовьте новую редакцию положения или подтвердите действующую."
        )
        notify(
            recipients, Event.REGULATION_REVIEW, title, body,
            reverse("committees:detail", args=[commission.pk]) + "?tab=regulations", commission,
        )
        reg.review_reminder_sent = True
        reg.save(update_fields=["review_reminder_sent"])
        sent += 1
    return sent


def run(now=None):
    """Ежедневное задание."""
    return {"regulation_review_reminders": send_regulation_review_reminders(now)}


def run_monthly_legal_check(now=None):
    """Ежемесячная проверка всех НПА со ссылкой (кроме «замена не требуется»)."""
    from .adilet import check_acts, checkable_acts

    return check_acts(checkable_acts(), manual=False)
