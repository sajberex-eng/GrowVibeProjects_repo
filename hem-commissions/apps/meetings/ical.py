"""Экспорт заседаний в iCalendar (RFC 5545). Без данных пациентов и вопросов повестки."""
from datetime import timezone as dt_timezone
from urllib.parse import urlparse

from django.conf import settings
from django.http import HttpResponse
from django.utils import timezone

from .models import Meeting

PRODID = "-//Center of Hematology//Commissions//RU"


def _escape(value):
    return (
        str(value or "")
        .replace("\\", "\\\\")
        .replace(";", "\\;")
        .replace(",", "\\,")
        .replace("\r\n", "\\n")
        .replace("\n", "\\n")
    )


def _fold(line):
    """Перенос строк длиннее 75 октетов."""
    data = line.encode("utf-8")
    if len(data) <= 75:
        return line
    parts, current = [], b""
    for ch in line:
        encoded = ch.encode("utf-8")
        limit = 75 if not parts else 74
        if len(current) + len(encoded) > limit:
            parts.append(current.decode("utf-8"))
            current = b""
        current += encoded
    parts.append(current.decode("utf-8"))
    return "\r\n ".join(parts)


def _utc(dt):
    return dt.astimezone(dt_timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _host():
    return urlparse(settings.SITE_URL).hostname or "commissions.local"


def event_lines(meeting):
    from .services import meeting_url

    url = settings.SITE_URL + meeting_url(meeting)
    summary = f"Заседание {meeting.commission.short_name}"
    if meeting.kind == Meeting.Kind.EXTRA:
        summary += " (внеочередное)"
    if meeting.status == Meeting.Status.CANCELLED:
        summary = "ОТМЕНЕНО: " + summary
    description = [meeting.commission.name, f"Формат: {meeting.get_format_display()}"]
    if meeting.video_link:
        description.append(f"Видеоконференция: {meeting.video_link}")
    description.append(f"Повестка и материалы: {url}")
    location = meeting.place or (meeting.video_link if meeting.format == Meeting.Format.ONLINE else "")
    status = {
        Meeting.Status.CANCELLED: "CANCELLED",
    }.get(meeting.status, "CONFIRMED")
    lines = [
        "BEGIN:VEVENT",
        f"UID:meeting-{meeting.pk}@{_host()}",
        f"DTSTAMP:{_utc(timezone.now())}",
        f"DTSTART:{_utc(meeting.starts_at)}",
        f"DTEND:{_utc(meeting.ends_at)}",
        f"SUMMARY:{_escape(summary)}",
        f"DESCRIPTION:{_escape(chr(10).join(description))}",
        f"URL:{url}",
        f"STATUS:{status}",
        f"SEQUENCE:{meeting.reschedules.count()}",
    ]
    if location:
        lines.append(f"LOCATION:{_escape(location)}")
    if meeting.status != Meeting.Status.CANCELLED:
        lines += [
            "BEGIN:VALARM", "ACTION:DISPLAY", "TRIGGER:-PT1H",
            f"DESCRIPTION:{_escape(summary)}", "END:VALARM",
        ]
    lines.append("END:VEVENT")
    return lines


def build_calendar(meetings, name="Заседания комиссий"):
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        f"PRODID:{PRODID}",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        f"X-WR-CALNAME:{_escape(name)}",
        f"X-WR-TIMEZONE:{settings.TIME_ZONE}",
    ]
    for meeting in meetings:
        lines.extend(event_lines(meeting))
    lines.append("END:VCALENDAR")
    return "\r\n".join(_fold(line) for line in lines) + "\r\n"


def ics_response(meetings, filename, name="Заседания комиссий"):
    response = HttpResponse(build_calendar(meetings, name), content_type="text/calendar; charset=utf-8")
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    return response
