import io
from datetime import timedelta

from django.core.files.base import ContentFile
from django.core.management import call_command
from django.utils import timezone

from apps.accounts.models import City, User
from apps.committees import jobs
from apps.committees.models import (
    AbsenceReason, Commission, Holiday, LegalAct, MemberRecord, Order, Regulation, Rubric,
)
from apps.notifications.models import Notification

from .base import CommissionTestCase, MediaTestCase


class SeedInitialTests(MediaTestCase):
    def counts(self):
        return [m.objects.count() for m in (City, AbsenceReason, Holiday, Commission, LegalAct, Rubric)]

    def test_idempotent(self):
        call_command("seed_initial", stdout=io.StringIO())
        first = self.counts()
        call_command("seed_initial", stdout=io.StringIO())
        self.assertEqual(first, self.counts())
        self.assertEqual(Commission.objects.filter(kind=Commission.Kind.CORE).count(), 7)
        self.assertEqual(Commission.objects.filter(kind=Commission.Kind.RECOMMENDED, is_active=False).count(), 6)
        self.assertTrue(Commission.objects.get(short_name="ОСК").handles_patient_cases)
        self.assertFalse(Commission.objects.get(short_name="ВКК").handles_patient_cases)
        self.assertTrue(Commission.objects.get(short_name="КИЛИ").handles_patient_cases)
        self.assertFalse(Commission.objects.get(short_name="ЛЭК").is_active)
        self.assertFalse(AbsenceReason.objects.get(name="Без уважительной причины").is_excused)
        self.assertEqual(Holiday.objects.filter(date__year=2026).count(), 15)
        self.assertFalse(LegalAct.objects.exclude(status=LegalAct.Status.NEEDS_CHECK).exists())

    def test_demo(self):
        out = io.StringIO()
        call_command("seed_initial", "--demo", stdout=out)
        call_command("seed_initial", "--demo", stdout=io.StringIO())
        self.assertIn("Demo-2026-pass", out.getvalue())
        admin = User.objects.get(username="admin")
        self.assertTrue(admin.is_staff)
        self.assertTrue(admin.check_password("Demo-2026-pass"))
        for short in ("ЕФК", "ОСК"):
            c = Commission.objects.get(short_name=short)
            self.assertEqual(c.members_on().count(), 7)
            self.assertEqual(c.members_on().filter(role=MemberRecord.Role.CHAIRMAN).count(), 1)
            self.assertEqual(c.orders.filter(status=Order.Status.SIGNED).count(), 1)
            self.assertEqual(c.meetings.count(), 2)


class RegulationReminderTests(CommissionTestCase):
    def test_reminder_sent_once(self):
        today = timezone.localdate()
        reg = Regulation.objects.create(
            commission=self.commission, approved_on=today - timedelta(days=300),
            review_on=today + timedelta(days=20), file=ContentFile(b"x", name="r.pdf"),
        )
        Regulation.objects.create(
            commission=self.commission, version=0, approved_on=today - timedelta(days=900),
            review_on=today, expired_on=today - timedelta(days=300), file=ContentFile(b"x", name="old.pdf"),
        )
        self.assertEqual(jobs.run(), {"regulation_review_reminders": 1})
        self.assertEqual(jobs.run(), {"regulation_review_reminders": 0})
        reg.refresh_from_db()
        self.assertTrue(reg.review_reminder_sent)
        users = set(Notification.objects.filter(event="regulation_review").values_list("user_id", flat=True))
        self.assertEqual(users, {self.chairman.pk, self.secretary.pk})

    def test_not_yet(self):
        today = timezone.localdate()
        Regulation.objects.create(
            commission=self.commission, approved_on=today, review_on=today + timedelta(days=40),
            file=ContentFile(b"x", name="r.pdf"),
        )
        self.assertEqual(jobs.run()["regulation_review_reminders"], 0)
