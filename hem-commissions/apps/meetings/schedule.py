"""Генератор графика заседаний на год.

Правила:
  * nth_weekday — ежемесячно в N-й день недели (1–4 или последний), напр. «каждый второй четверг»;
  * day_of_month — ежемесячно в заданное число (выходной/праздник → следующий рабочий день);
  * quarterly — ежеквартально в N-й день недели первого/второго/третьего месяца квартала.
"""
import calendar
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from django.utils import timezone

RULE_NTH_WEEKDAY = "nth_weekday"
RULE_DAY_OF_MONTH = "day_of_month"
RULE_QUARTERLY = "quarterly"

RULE_CHOICES = [
    (RULE_NTH_WEEKDAY, "Ежемесячно: N-й день недели"),
    (RULE_DAY_OF_MONTH, "Ежемесячно: число месяца"),
    (RULE_QUARTERLY, "Ежеквартально: N-й день недели"),
]
WEEKDAY_CHOICES = [
    (0, "понедельник"), (1, "вторник"), (2, "среда"), (3, "четверг"), (4, "пятница"),
    (5, "суббота"), (6, "воскресенье"),
]
LAST = -1
NTH_CHOICES = [(1, "первый"), (2, "второй"), (3, "третий"), (4, "четвёртый"), (LAST, "последний")]
QUARTER_MONTH_CHOICES = [(1, "первый месяц квартала"), (2, "второй месяц квартала"), (3, "третий месяц квартала")]


def nth_weekday(year, month, weekday, n):
    """Дата N-го дня недели месяца (n = 1..5 или -1 — последний). None, если такой даты нет."""
    if n == LAST:
        last_day = calendar.monthrange(year, month)[1]
        d = date(year, month, last_day)
        return d - timedelta(days=(d.weekday() - weekday) % 7)
    if not 1 <= n <= 5:
        raise ValueError("n должен быть 1..5 или -1")
    first = date(year, month, 1)
    d = first + timedelta(days=(weekday - first.weekday()) % 7) + timedelta(weeks=n - 1)
    return d if d.month == month else None


def _workday_checker(year):
    from apps.committees.models import Holiday
    from apps.committees.workdays import is_workday

    overrides = dict(
        Holiday.objects.filter(date__gte=date(year, 1, 1), date__lte=date(year + 1, 1, 31))
        .values_list("date", "is_working_day")
    )
    return lambda d: is_workday(d, overrides)


def shift_to_workday(d, is_workday):
    while not is_workday(d):
        d += timedelta(days=1)
    return d


def generate_dates(year, rule, weekday=None, n=None, day=None, quarter_month=1, is_workday=None):
    """Список дат заседаний на год по правилу."""
    dates = []
    if rule == RULE_NTH_WEEKDAY:
        for month in range(1, 13):
            d = nth_weekday(year, month, weekday, n)
            if d:
                dates.append(d)
    elif rule == RULE_QUARTERLY:
        if quarter_month not in (1, 2, 3):
            raise ValueError("quarter_month должен быть 1..3")
        for q in range(4):
            d = nth_weekday(year, q * 3 + quarter_month, weekday, n)
            if d:
                dates.append(d)
    elif rule == RULE_DAY_OF_MONTH:
        if not day or not 1 <= day <= 31:
            raise ValueError("Число месяца должно быть 1..31")
        if is_workday is None:
            is_workday = _workday_checker(year)
        for month in range(1, 13):
            d = date(year, month, min(day, calendar.monthrange(year, month)[1]))
            dates.append(shift_to_workday(d, is_workday))
    else:
        raise ValueError(f"Неизвестное правило: {rule}")
    return dates


def describe_rule(rule, weekday=None, n=None, day=None, quarter_month=1):
    wd = dict(WEEKDAY_CHOICES).get(weekday, "")
    nth = dict(NTH_CHOICES).get(n, "")
    if rule == RULE_NTH_WEEKDAY:
        return f"ежемесячно, {nth} {wd}"
    if rule == RULE_QUARTERLY:
        return f"ежеквартально, {nth} {wd}, {dict(QUARTER_MONTH_CHOICES).get(quarter_month, '')}"
    return f"ежемесячно, {day}-го числа (с переносом на рабочий день)"


@dataclass
class PlannedSlot:
    date: date
    starts_at: datetime
    exists: bool
    in_past: bool

    @property
    def will_create(self):
        return not self.exists and not self.in_past


def build_slots(commission, dates, start_time: time):
    """Слоты графика с отметкой «уже есть заседание в этот день»."""
    from .models import Meeting

    busy = set()
    if dates:
        for starts_at in (
            Meeting.objects.filter(commission=commission, starts_at__date__gte=min(dates), starts_at__date__lte=max(dates))
            .exclude(status=Meeting.Status.CANCELLED)
            .values_list("starts_at", flat=True)
        ):
            busy.add(timezone.localtime(starts_at).date())
    now = timezone.now()
    slots = []
    for d in dates:
        starts_at = timezone.make_aware(datetime.combine(d, start_time))
        slots.append(PlannedSlot(date=d, starts_at=starts_at, exists=d in busy, in_past=starts_at <= now))
    return slots
