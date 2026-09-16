from django import template
from django.urls import NoReverseMatch, reverse
from django.utils import timezone

register = template.Library()


@register.simple_tag
def safe_url(name, *args):
    """Как {% url %}, но возвращает пустую строку, если маршрут не зарегистрирован."""
    try:
        return reverse(name, args=[a for a in args if a is not None])
    except NoReverseMatch:
        return ""


@register.filter
def time_left(deadline):
    """«осталось 2 дн. 3 ч.» / «срок истёк»."""
    if not deadline:
        return ""
    delta = deadline - timezone.now()
    seconds = int(delta.total_seconds())
    if seconds <= 0:
        return "срок истёк"
    days, rest = divmod(seconds, 86400)
    hours, rest = divmod(rest, 3600)
    minutes = rest // 60
    if days:
        return f"осталось {days} дн. {hours} ч."
    if hours:
        return f"осталось {hours} ч. {minutes} мин."
    return f"осталось {max(minutes, 1)} мин."
