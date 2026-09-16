"""Периодические задачи протоколов (запускать ежедневно, можно чаще — повторных писем нет).

* согласование: напоминание не ответившим за 1 день до срока; после срока —
  автоматическая передача на подпись и сводка председателю;
* поручения: напоминания за 7 и за 1 день до срока; в первый день просрочки —
  уведомление ответственному, соисполнителям и председателю.
"""
import logging
from datetime import timedelta

from django.utils import timezone

from apps.committees.models import MemberRecord
from apps.notifications.models import Event
from apps.notifications.services import notify

from . import services
from .models import Assignment, Protocol

logger = logging.getLogger(__name__)


def _approvals(today, stats):
    for protocol in Protocol.objects.filter(status=Protocol.Status.APPROVAL, approval_deadline__isnull=False).select_related("commission"):
        try:
            if protocol.approval_deadline < today:
                services.move_to_signing(protocol, auto=True)
                stats["moved_to_signing"] += 1
            elif not protocol.approval_reminder_sent and today >= protocol.approval_deadline - timedelta(days=1):
                waiting = services.non_responders(protocol)
                notify(
                    waiting, Event.PROTOCOL_APPROVAL,
                    f"Напоминание: согласование протокола — {protocol.commission.short_name}",
                    (
                        f"Срок согласования протокола № {protocol.number} истекает "
                        f"{protocol.approval_deadline:%d.%m.%Y}. Отсутствие ответа до окончания срока "
                        "означает отсутствие замечаний."
                    ),
                    services.protocol_url(protocol), protocol.commission,
                )
                protocol.approval_reminder_sent = True
                protocol.save(update_fields=["approval_reminder_sent"])
                stats["approval_reminders"] += len(waiting)
        except Exception:  # noqa: BLE001 — одна ошибка не должна останавливать остальные протоколы
            logger.exception("Protocol job failed for %s", protocol.pk)
            stats["errors"] += 1


def _assignments(today, stats):
    qs = (
        Assignment.objects.filter(
            status__in=Assignment.OPEN_STATUSES, decision__protocol__status=Protocol.Status.SIGNED,
        )
        .select_related("commission", "responsible", "decision__protocol")
        .prefetch_related("co_executors")
    )
    for a in qs:
        days_left = (a.due_date - today).days
        number = a.decision.protocol.number
        commission = a.commission
        if days_left < 0:
            if a.overdue_notified:
                continue
            if a.status == Assignment.Status.ON_CONFIRMATION:
                continue  # исполнение уже отмечено, ждёт подтверждения
            services.notify_assignment(
                a, f"Поручение просрочено — {commission.short_name}",
                f"Истёк срок ({a.due_date:%d.%m.%Y}) исполнения поручения по протоколу № {number}.",
            )
            chairman = commission.officer(MemberRecord.Role.CHAIRMAN)
            notify(
                chairman, Event.ASSIGNMENT, f"Просрочено поручение — {commission.short_name}",
                f"Поручение по протоколу № {number} (ответственный: {a.responsible_display}) не исполнено в срок "
                f"{a.due_date:%d.%m.%Y}.",
                services.assignment_url(a), commission,
            )
            a.overdue_notified = True
            a.reminded_7d = a.reminded_1d = True
            a.save(update_fields=["overdue_notified", "reminded_7d", "reminded_1d"])
            stats["overdue"] += 1
        elif days_left <= 1:
            if a.reminded_1d or a.status == Assignment.Status.ON_CONFIRMATION:
                continue
            services.notify_assignment(
                a, f"Срок поручения — {'сегодня' if days_left == 0 else 'завтра'} ({commission.short_name})",
                f"Срок исполнения поручения по протоколу № {number}: {a.due_date:%d.%m.%Y}.",
            )
            a.reminded_1d = a.reminded_7d = True
            a.save(update_fields=["reminded_7d", "reminded_1d"])
            stats["reminders_1d"] += 1
        elif days_left <= 7:
            if a.reminded_7d or a.status == Assignment.Status.ON_CONFIRMATION:
                continue
            services.notify_assignment(
                a, f"Напоминание о сроке поручения — {commission.short_name}",
                f"До срока исполнения поручения по протоколу № {number} осталось {days_left} дн. "
                f"(срок {a.due_date:%d.%m.%Y}).",
            )
            a.reminded_7d = True
            a.save(update_fields=["reminded_7d"])
            stats["reminders_7d"] += 1


def run(now=None):
    now = now or timezone.now()
    today = timezone.localdate(now)
    stats = {
        "moved_to_signing": 0, "approval_reminders": 0, "reminders_7d": 0, "reminders_1d": 0,
        "overdue": 0, "errors": 0,
    }
    _approvals(today, stats)
    _assignments(today, stats)
    return stats
