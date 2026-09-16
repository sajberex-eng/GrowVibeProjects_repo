"""Отправка уведомлений: внутри системы (всегда), e-mail, WhatsApp (через n8n).

Правило: в тексте уведомлений не передаются данные пациентов — только
наименование комиссии, заседания/документа и ссылка в систему.
"""
import json
import logging
import urllib.request

from django.conf import settings
from django.core.mail import send_mail
from django.utils import timezone

from .models import CRITICAL_EVENTS, Notification, NotificationPreference

logger = logging.getLogger(__name__)


def absolute_url(path):
    if not path:
        return settings.SITE_URL + "/"
    if path.startswith("http"):
        return path
    return settings.SITE_URL + path


def _preferences(user, event):
    pref = NotificationPreference.objects.filter(user=user, event=event).first()
    email = pref.email if pref else True
    whatsapp = pref.whatsapp if pref else True
    if event in CRITICAL_EVENTS:
        email = True
    return email, whatsapp


def send_whatsapp(phone, text):
    url = settings.N8N_WHATSAPP_WEBHOOK_URL
    if not url or not phone:
        return False
    data = json.dumps({"phone": phone, "text": text}).encode()
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return 200 <= resp.status < 300
    except OSError:
        logger.exception("WhatsApp webhook failed")
        return False


def notify(users, event, title, body="", url="", commission=None, email_subject=None):
    """Создаёт уведомления и рассылает их по включённым каналам.

    users — пользователь или итерируемое; дубликаты и неактивные отбрасываются.
    Возвращает список созданных Notification.
    """
    if users is None:
        return []
    if hasattr(users, "pk"):
        users = [users]
    seen = set()
    created = []
    link = absolute_url(url)
    for user in users:
        if user is None or user.pk in seen or not user.is_active:
            continue
        seen.add(user.pk)
        note = Notification.objects.create(
            user=user, event=event, title=title[:300], body=body, url=url, commission=commission
        )
        want_email, want_whatsapp = _preferences(user, event)
        if want_email and user.email:
            text = f"{body}\n\nОткрыть в системе: {link}\n\n—\nИС «Комиссии», {settings.ORGANIZATION_NAME}".strip()
            try:
                send_mail(email_subject or title, text, settings.DEFAULT_FROM_EMAIL, [user.email])
                note.emailed_at = timezone.now()
            except Exception:  # noqa: BLE001 — сбой почты не должен ломать бизнес-операцию
                logger.exception("Email notification failed for user %s", user.pk)
        if (
            want_whatsapp
            and user.whatsapp_opt_in
            and commission is not None
            and commission.whatsapp_enabled
            and send_whatsapp(user.phone, f"{title}\n{link}")
        ):
            note.whatsapp_sent_at = timezone.now()
        if note.emailed_at or note.whatsapp_sent_at:
            note.save(update_fields=["emailed_at", "whatsapp_sent_at"])
        created.append(note)
    return created


def notify_admins(event, title, body="", url=""):
    from apps.accounts.models import User

    return notify(User.objects.filter(is_active=True, is_staff=True), event, title, body, url)
