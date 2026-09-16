"""Расчёт рабочих дней по производственному календарю РК."""
from datetime import timedelta

from .models import Holiday


def is_workday(day, overrides=None):
    if overrides is None:
        overrides = dict(Holiday.objects.filter(date=day).values_list("date", "is_working_day"))
    if day in overrides:
        return overrides[day]
    return day.weekday() < 5


def add_workdays(start, days):
    """Дата, наступающая через `days` рабочих дней после `start`."""
    overrides = dict(
        Holiday.objects.filter(date__gt=start, date__lte=start + timedelta(days=days * 3 + 30))
        .values_list("date", "is_working_day")
    )
    current = start
    remaining = days
    while remaining > 0:
        current += timedelta(days=1)
        if is_workday(current, overrides):
            remaining -= 1
    return current


def workdays_between(start, end):
    """Число рабочих дней в интервале (start, end]."""
    if end <= start:
        return 0
    overrides = dict(Holiday.objects.filter(date__gt=start, date__lte=end).values_list("date", "is_working_day"))
    count = 0
    current = start
    while current < end:
        current += timedelta(days=1)
        if is_workday(current, overrides):
            count += 1
    return count
