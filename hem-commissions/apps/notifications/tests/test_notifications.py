import sys
import types
from datetime import datetime
from io import StringIO
from unittest import mock

from django.core.management import call_command
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from apps.committees.testing import make_user
from apps.notifications import scheduler
from apps.notifications.models import Event, JobRun, Notification
from apps.notifications.services import notify


class NotificationViewsTests(TestCase):
    def setUp(self):
        self.user = make_user()
        self.other = make_user()
        self.old = notify(self.user, Event.SYSTEM, "Старое", url="/votings/")[0]
        self.old.read_at = timezone.now()
        self.old.save()
        self.new = notify(self.user, Event.VOTING, "Новое голосование", "текст", url="/votings/5/")[0]
        self.foreign = notify(self.other, Event.SYSTEM, "Чужое")[0]
        self.client.force_login(self.user)

    def test_list_unread_first(self):
        response = self.client.get(reverse("notifications:list"))
        self.assertEqual(response.status_code, 200)
        titles = [n.title for n in response.context["page"]]
        self.assertEqual(titles, ["Новое голосование", "Старое"])
        self.assertNotContains(response, "Чужое")

    def test_filter_unread(self):
        response = self.client.get(reverse("notifications:list"), {"unread": "1"})
        self.assertEqual([n.title for n in response.context["page"]], ["Новое голосование"])

    def test_read_marks_and_redirects(self):
        response = self.client.get(reverse("notifications:read", args=[self.new.pk]))
        self.assertRedirects(response, "/votings/5/", fetch_redirect_response=False)
        self.new.refresh_from_db()
        self.assertIsNotNone(self.new.read_at)

    def test_read_external_url_not_followed(self):
        note = Notification.objects.create(user=self.user, event=Event.SYSTEM, title="x", url="https://evil.example/")
        response = self.client.get(reverse("notifications:read", args=[note.pk]))
        self.assertRedirects(response, reverse("notifications:list"), fetch_redirect_response=False)

    def test_cannot_read_foreign(self):
        response = self.client.get(reverse("notifications:read", args=[self.foreign.pk]))
        self.assertEqual(response.status_code, 404)

    def test_read_all(self):
        self.assertEqual(self.client.get(reverse("notifications:read_all")).status_code, 405)
        self.client.post(reverse("notifications:read_all"))
        self.assertFalse(Notification.objects.filter(user=self.user, read_at__isnull=True).exists())
        self.assertTrue(Notification.objects.filter(user=self.other, read_at__isnull=True).exists())


def fake_module(name, **funcs):
    module = types.ModuleType(name)
    for key, value in funcs.items():
        setattr(module, key, value)
    return module


class SchedulerTests(TestCase):
    def fake_modules(self):
        self.committees_run = mock.Mock(return_value={"ok": 1})
        self.monthly = mock.Mock(return_value={"checked": 3})
        self.meetings_run = mock.Mock(side_effect=RuntimeError("boom"))
        self.voting_run = mock.Mock(return_value={"closed": 0})
        return {
            "apps.committees.jobs": fake_module("apps.committees.jobs", run=self.committees_run, run_monthly_legal_check=self.monthly),
            "apps.meetings.jobs": fake_module("apps.meetings.jobs", run=self.meetings_run),
            "apps.voting.jobs": fake_module("apps.voting.jobs", run=self.voting_run),
        }

    def test_run_daily_calls_all_and_tolerates_failures(self):
        modules = self.fake_modules()
        with mock.patch.dict(sys.modules, modules), \
                mock.patch.object(scheduler, "DAILY_JOBS", scheduler.DAILY_JOBS + [("apps.nonexistent.jobs", "run")]):
            with self.assertLogs("apps.notifications.scheduler", level="WARNING"):
                result = scheduler.run_daily()
        self.committees_run.assert_called_once()
        self.meetings_run.assert_called_once()
        self.voting_run.assert_called_once()
        self.assertEqual(result["apps.committees.jobs.run"], {"ok": 1})
        self.assertIn("error", result["apps.meetings.jobs.run"])
        self.assertIn("skipped", result["apps.nonexistent.jobs.run"])

    def test_missing_module_logged(self):
        with mock.patch.object(scheduler, "DAILY_JOBS", [("apps.nonexistent.jobs", "run")]):
            with self.assertLogs("apps.notifications.scheduler", level="WARNING") as logs:
                result = scheduler.run_daily()
        self.assertEqual(result, {"apps.nonexistent.jobs.run": {"skipped": "module not found"}})
        self.assertTrue(any("не найден" in line for line in logs.output))

    def test_run_monthly(self):
        with mock.patch.dict(sys.modules, self.fake_modules()):
            result = scheduler.run_monthly()
        self.monthly.assert_called_once()
        self.assertEqual(result["apps.committees.jobs.run_monthly_legal_check"], {"checked": 3})

    def test_tick_runs_daily_once_after_seven(self):
        modules = self.fake_modules()
        early = timezone.make_aware(datetime(2026, 9, 15, 6, 30))
        morning = timezone.make_aware(datetime(2026, 9, 15, 7, 15))
        later = timezone.make_aware(datetime(2026, 9, 15, 12, 0))
        next_day = timezone.make_aware(datetime(2026, 9, 16, 7, 5))
        with mock.patch.dict(sys.modules, modules), self.assertLogs("apps.notifications.scheduler", level="ERROR"):
            self.assertNotIn("daily", scheduler.tick(early))
            self.assertIn("daily", scheduler.tick(morning))
            self.assertNotIn("daily", scheduler.tick(later))
            self.assertIn("daily", scheduler.tick(next_day))
        self.assertEqual(self.committees_run.call_count, 2)
        # частая задача (голосования) — на каждом проходе, плюс в ежедневном наборе
        self.assertEqual(self.voting_run.call_count, 6)
        self.assertEqual(JobRun.objects.get(name="daily").last_run, next_day)
        self.monthly.assert_not_called()

    def test_tick_monthly_on_first_day(self):
        modules = self.fake_modules()
        first = timezone.make_aware(datetime(2026, 10, 1, 8, 0))
        with mock.patch.dict(sys.modules, modules), self.assertLogs("apps.notifications.scheduler", level="ERROR"):
            self.assertIn("monthly", scheduler.tick(first))
            self.assertNotIn("monthly", scheduler.tick(first.replace(hour=9)))
        self.monthly.assert_called_once()

    def test_commands(self):
        out = StringIO()
        with mock.patch.dict(sys.modules, self.fake_modules()), self.assertLogs("apps.notifications.scheduler", level="ERROR"):
            call_command("run_daily_jobs", "--monthly", stdout=out)
            call_command("run_scheduler", "--once", stdout=out)
        self.assertIn("apps.committees.jobs.run_monthly_legal_check", out.getvalue())
        self.monthly.assert_called()

    def test_real_voting_job_runs(self):
        key, result = scheduler.call_job("apps.voting.jobs", "run")
        self.assertEqual(result, {"closed": 0, "reminded": 0})
