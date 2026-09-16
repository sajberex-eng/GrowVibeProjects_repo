"""Периодические задачи заседаний (вызывается планировщиком).

run(now=None) -> dict — напоминания за 3 дня и за 1 день до заседания.
"""
from datetime import timedelta

from django.utils import timezone

from apps.notifications.models import Event
from apps.notifications.services import notify

from .models import Meeting


def _remind(meeting, when_text):
    from . import services

    notify(
        services.meeting_recipients(meeting), Event.MEETING_REMINDER,
        f"Напоминание: {when_text} заседание {meeting.commission.short_name} {services.local_str(meeting.starts_at)}",
        services._details(meeting) + "\nОзнакомьтесь с повесткой и материалами заранее.",
        services.meeting_url(meeting), meeting.commission,
    )


def run(now=None):
    from . import services

    now = now or timezone.now()
    result = {"reminders_3d": 0, "reminders_1d": 0, "control_items_refreshed": 0}
    qs = Meeting.objects.filter(
        status__in=[Meeting.Status.PLANNED, Meeting.Status.POSTPONED],
        starts_at__gt=now,
        starts_at__lte=now + timedelta(days=3),
    ).select_related("commission")
    for meeting in qs:
        if meeting.starts_at <= now + timedelta(days=1):
            if meeting.reminder_1d_sent:
                continue
            _remind(meeting, "завтра" if timezone.localtime(meeting.starts_at).date() > timezone.localtime(now).date() else "сегодня")
            Meeting.objects.filter(pk=meeting.pk).update(reminder_1d_sent=True, reminder_3d_sent=True)
            result["reminders_1d"] += 1
        elif not meeting.reminder_3d_sent:
            if meeting.agenda_status == Meeting.AgendaStatus.DRAFT:
                services.ensure_control_item(meeting)
                result["control_items_refreshed"] += 1
            days = (timezone.localtime(meeting.starts_at).date() - timezone.localtime(now).date()).days
            _remind(meeting, f"через {days} дн." if days > 1 else "скоро")
            Meeting.objects.filter(pk=meeting.pk).update(reminder_3d_sent=True)
            result["reminders_3d"] += 1
    return result
