from datetime import date, datetime, time, timedelta

from django.test import SimpleTestCase
from django.urls import reverse
from django.utils import timezone

from apps.committees.models import Holiday
from apps.meetings import schedule
from apps.meetings.models import Meeting

from .base import MeetingTestCase

THU = 3
FRI = 4


def weekdays_only(d):
    return d.weekday() < 5


class NthWeekdayTests(SimpleTestCase):
    def test_second_thursday(self):
        # 01.01.2026 — четверг
        self.assertEqual(schedule.nth_weekday(2026, 1, THU, 1), date(2026, 1, 1))
        self.assertEqual(schedule.nth_weekday(2026, 1, THU, 2), date(2026, 1, 8))
        self.assertEqual(schedule.nth_weekday(2026, 2, THU, 2), date(2026, 2, 12))

    def test_last_weekday(self):
        self.assertEqual(schedule.nth_weekday(2026, 1, FRI, schedule.LAST), date(2026, 1, 30))
        self.assertEqual(schedule.nth_weekday(2026, 7, FRI, schedule.LAST), date(2026, 7, 31))

    def test_fifth_missing(self):
        self.assertIsNone(schedule.nth_weekday(2026, 2, THU, 5))

    def test_monthly_rule(self):
        dates = schedule.generate_dates(2026, schedule.RULE_NTH_WEEKDAY, weekday=THU, n=2)
        self.assertEqual(len(dates), 12)
        self.assertTrue(all(d.weekday() == THU and 8 <= d.day <= 14 for d in dates))

    def test_quarterly_rule(self):
        dates = schedule.generate_dates(2026, schedule.RULE_QUARTERLY, weekday=THU, n=schedule.LAST, quarter_month=2)
        self.assertEqual([d.month for d in dates], [2, 5, 8, 11])
        self.assertEqual(dates[0], date(2026, 2, 26))
        first = schedule.generate_dates(2026, schedule.RULE_QUARTERLY, weekday=THU, n=1, quarter_month=1)
        self.assertEqual([d.month for d in first], [1, 4, 7, 10])

    def test_day_of_month_shifts_to_workday(self):
        dates = schedule.generate_dates(2026, schedule.RULE_DAY_OF_MONTH, day=3, is_workday=weekdays_only)
        self.assertEqual(dates[0], date(2026, 1, 5))  # 03.01.2026 — суббота
        self.assertEqual(len(dates), 12)
        self.assertTrue(all(d.weekday() < 5 for d in dates))

    def test_day_31_clamped_to_month_end(self):
        dates = schedule.generate_dates(2026, schedule.RULE_DAY_OF_MONTH, day=31, is_workday=weekdays_only)
        # 28.02.2026 — суббота → 02.03.2026
        self.assertEqual(dates[1], date(2026, 3, 2))
        self.assertEqual(dates[3], date(2026, 4, 30))

    def test_invalid(self):
        with self.assertRaises(ValueError):
            schedule.generate_dates(2026, "weekly")
        with self.assertRaises(ValueError):
            schedule.generate_dates(2026, schedule.RULE_DAY_OF_MONTH, day=0, is_workday=weekdays_only)


class ScheduleDbTests(MeetingTestCase):
    def test_holidays_from_calendar(self):
        Holiday.objects.create(date=date(2027, 3, 8), name="8 марта")
        Holiday.objects.create(date=date(2027, 3, 9), name="перенос")
        dates = schedule.generate_dates(2027, schedule.RULE_DAY_OF_MONTH, day=8)
        # 08.03.2027 — понедельник, праздник; 09.03 — тоже выходной → 10.03
        self.assertEqual(dates[2], date(2027, 3, 10))

    def test_working_saturday(self):
        Holiday.objects.create(date=date(2027, 1, 2), name="рабочая суббота", is_working_day=True)
        dates = schedule.generate_dates(2027, schedule.RULE_DAY_OF_MONTH, day=2)
        self.assertEqual(dates[0], date(2027, 1, 2))

    def test_slots_mark_existing(self):
        year = timezone.localdate().year + 1
        dates = schedule.generate_dates(year, schedule.RULE_NTH_WEEKDAY, weekday=THU, n=2)
        existing = timezone.make_aware(datetime.combine(dates[0], time(10, 0)))
        self.meeting(starts_at=existing)
        slots = schedule.build_slots(self.commission, dates, time(14, 0))
        self.assertTrue(slots[0].exists)
        self.assertFalse(slots[0].will_create)
        self.assertTrue(all(s.will_create for s in slots[1:]))

    def test_schedule_view_creates_meetings(self):
        year = timezone.localdate().year + 1
        first = schedule.nth_weekday(year, 1, THU, 2)
        self.meeting(starts_at=timezone.make_aware(datetime.combine(first, time(9, 0))))
        self.login(self.secretary)
        url = reverse("meetings:schedule", args=[self.commission.pk])
        data = {
            "year": year, "rule": "nth_weekday", "nth": 2, "weekday": THU, "quarter_month": 1,
            "start_time": "14:00", "duration_minutes": 90, "format": "offline", "place": "Конференц-зал",
        }
        resp = self.client.post(url, {**data, "action": "preview"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.context["to_create"], 11)
        resp = self.client.post(url, {**data, "action": "create"})
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(Meeting.objects.filter(commission=self.commission, starts_at__year=year).count(), 12)
        created = Meeting.objects.filter(commission=self.commission, starts_at__year=year).order_by("starts_at")[1]
        self.assertEqual(timezone.localtime(created.starts_at).time(), time(14, 0))
        self.assertEqual(created.place, "Конференц-зал")

    def test_schedule_view_forbidden_for_member(self):
        self.login(self.member)
        resp = self.client.get(reverse("meetings:schedule", args=[self.commission.pk]))
        self.assertEqual(resp.status_code, 403)
