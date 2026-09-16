from django import template
from django.utils import timezone
from django.utils.html import format_html

register = template.Library()

STATUS_COLORS = {
    # общие
    "draft": "gray", "project": "gray", "cancelled": "gray", "retired": "gray",
    # приказы / состав
    "signed": "green", "applied": "green",
    # НПА
    "actual": "green", "lost_force": "red", "unavailable": "red", "needs_check": "amber",
    # заседания
    "planned": "blue", "held": "green", "postponed": "amber",
    "approved": "green",
    # протоколы
    "approval": "amber", "signing": "blue", "annulled": "red",
    # голосования
    "open": "blue", "closed": "gray", "adopted": "green", "rejected": "red", "no_quorum": "amber",
    # поручения
    "assigned": "blue", "in_progress": "blue", "on_confirmation": "amber",
    "done": "green", "done_late": "amber", "overdue": "red",
    # согласования
    "pending": "gray", "agreed": "green", "remarks": "amber", "ack": "green", "suggestion": "amber",
    "new": "blue", "accepted": "green",
    # присутствие
    "present": "green", "online": "green", "absent_excused": "amber", "absent": "red",
}


@register.simple_tag
def status_badge(obj, field="status", override=None):
    """{% status_badge obj %} — бейдж по полю со choices."""
    value = override or getattr(obj, field)
    display = getattr(obj, f"get_{field}_display", lambda: value)()
    if override == "overdue":
        display = "Просрочено"
    color = STATUS_COLORS.get(value, "gray")
    return format_html('<span class="badge badge-{}">{}</span>', color, display)


@register.simple_tag
def badge(text, color="gray"):
    return format_html('<span class="badge badge-{}">{}</span>', color, text)


@register.simple_tag
def open_votes_count(user):
    if not user.is_authenticated:
        return 0
    from apps.voting.models import VotingSession

    return (
        VotingSession.objects.filter(status=VotingSession.Status.OPEN, eligible=user, deadline__gt=timezone.now())
        .exclude(votes__user=user)
        .count()
    )


@register.filter
def local_dt(value, fmt="%d.%m.%Y %H:%M"):
    if not value:
        return "—"
    if hasattr(value, "tzinfo") and value.tzinfo is not None:
        value = timezone.localtime(value)
    return value.strftime(fmt)


@register.filter
def filesize(value):
    size = float(value or 0)
    for unit in ["Б", "КБ", "МБ", "ГБ"]:
        if size < 1024:
            return f"{size:.0f} {unit}" if unit == "Б" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} ТБ"


@register.filter
def get_item(mapping, key):
    try:
        return mapping.get(key)
    except AttributeError:
        return None


@register.simple_tag(takes_context=True)
def query_replace(context, **kwargs):
    """Ссылка с заменой параметров текущего запроса (для фильтров и пагинации)."""
    params = context["request"].GET.copy()
    for key, value in kwargs.items():
        if value in (None, ""):
            params.pop(key, None)
        else:
            params[key] = value
    return "?" + params.urlencode() if params else "?"
