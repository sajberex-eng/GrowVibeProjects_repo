from datetime import datetime, time, timedelta

from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse
from django.utils import timezone

from apps.committees.testing import make_user
from apps.meetings.models import Invitation
from apps.notifications.models import Event, Notification

from .. import jobs, services
from ..models import Assignment, AssignmentComment, Protocol
from .base import ProtocolTestCase


class AssignmentFlowTests(ProtocolTestCase):
    def setUp(self):
        super().setUp()
        self.protocol = self.make_signed()
        self.assignment = Assignment.objects.get(decision__protocol=self.protocol)
        self.responsible = self.members[0]

    def test_full_flow_on_time(self):
        a = self.assignment
        with self.assertRaises(ValidationError):
            services.report_done(a, self.members[2], "Сделано")  # не исполнитель
        self.login(self.responsible)
        self.client.post(reverse("protocols:assignment_start", args=[a.pk]))
        self.assertEqual(self.reload(a).status, Assignment.Status.IN_PROGRESS)
        self.client.post(reverse("protocols:assignment_report", args=[a.pk]), {
            "text": "Отчёт подготовлен", "completed_on": timezone.localdate().isoformat(),
            "file": SimpleUploadedFile("report.pdf", b"%PDF-1.4", content_type="application/pdf"),
        })
        a.refresh_from_db()
        self.assertEqual(a.status, Assignment.Status.ON_CONFIRMATION)
        self.assertTrue(AssignmentComment.objects.filter(assignment=a, file_name="report.pdf").exists())
        self.assertTrue(Notification.objects.filter(user=self.secretary, title__startswith="Поручение ждёт").exists())
        # исполнитель не может подтвердить сам
        self.client.post(reverse("protocols:assignment_confirm", args=[a.pk]))
        self.assertEqual(self.reload(a).status, Assignment.Status.ON_CONFIRMATION)
        comment = AssignmentComment.objects.get(file_name="report.pdf")
        self.assertEqual(self.client.get(reverse("protocols:assignment_file", args=[a.pk, comment.pk])).status_code, 200)

        self.login(self.secretary)
        self.client.post(reverse("protocols:assignment_return", args=[a.pk]), {"text": "Приложите протокол"})
        a.refresh_from_db()
        self.assertEqual(a.status, Assignment.Status.IN_PROGRESS)
        self.assertIsNone(a.completed_on)
        self.assertTrue(Notification.objects.filter(user=self.members[1], title__startswith="Поручение возвращено").exists())

        services.report_done(a, self.members[1], "Дополнено")  # соисполнитель
        self.client.post(reverse("protocols:assignment_confirm", args=[a.pk]))
        a.refresh_from_db()
        self.assertEqual(a.status, Assignment.Status.DONE)
        self.assertEqual(a.confirmed_by, self.secretary)
        self.assertEqual(self.client.get(reverse("protocols:assignment_detail", args=[a.pk])).status_code, 200)

    def test_done_late(self):
        a = self.assignment
        a.due_date = timezone.localdate() - timedelta(days=3)
        a.save()
        services.report_done(a, self.responsible, "Сделано с опозданием")
        services.confirm_assignment(a, self.chairman)
        self.assertEqual(self.reload(a).status, Assignment.Status.DONE_LATE)

    def test_cancel(self):
        a = self.assignment
        with self.assertRaises(ValidationError):
            services.cancel_assignment(a, self.responsible, "Не хочу")
        with self.assertRaises(ValidationError):
            services.cancel_assignment(a, self.secretary, "")
        self.login(self.chairman)
        self.client.post(reverse("protocols:assignment_cancel", args=[a.pk]), {"reason": "Утратило актуальность"})
        a.refresh_from_db()
        self.assertEqual(a.status, Assignment.Status.CANCELLED)
        self.assertEqual(a.cancel_reason, "Утратило актуальность")

    def test_registry_filters(self):
        a = self.assignment
        other = Assignment.objects.create(
            decision=a.decision, text="Просроченное поручение", responsible=self.members[2],
            due_date=timezone.localdate() - timedelta(days=1),
        )
        self.login(self.secretary)
        url = reverse("protocols:assignments")
        page = self.client.get(url, {"status": "overdue"})
        self.assertEqual([x.pk for x in page.context["page"]], [other.pk])
        self.assertContains(page, "row-danger")
        self.assertContains(page, "Просрочено")
        page = self.client.get(url, {"responsible": self.members[0].pk})
        self.assertEqual([x.pk for x in page.context["page"]], [a.pk])
        page = self.client.get(url, {"due_from": timezone.localdate().isoformat()})
        self.assertEqual([x.pk for x in page.context["page"]], [a.pk])
        services.report_done(a, self.responsible, "Готово")
        page = self.client.get(url, {"status": "on_confirmation"})
        self.assertEqual([x.pk for x in page.context["page"]], [a.pk])

        self.login(self.members[1])  # соисполнитель
        page = self.client.get(url, {"mine": "1"})
        self.assertEqual([x.pk for x in page.context["page"]], [a.pk])
        self.login(self.outsider)
        outsider_page = self.client.get(url)
        self.assertEqual(len(outsider_page.context["page"]), 0)
        self.assertEqual(self.client.get(reverse("protocols:assignment_detail", args=[a.pk])).status_code, 403)

    def test_jobs_assignment_reminders_once(self):
        a = self.assignment
        due = a.due_date

        def at(day):
            return timezone.make_aware(datetime.combine(day, time(8, 0)))

        Notification.objects.all().delete()
        self.assertEqual(jobs.run(now=at(due - timedelta(days=9)))["reminders_7d"], 0)
        self.assertEqual(jobs.run(now=at(due - timedelta(days=7)))["reminders_7d"], 1)
        self.assertEqual(jobs.run(now=at(due - timedelta(days=6)))["reminders_7d"], 0)
        self.assertEqual(Notification.objects.filter(user=self.responsible, event=Event.ASSIGNMENT).count(), 1)
        self.assertEqual(Notification.objects.filter(user=self.members[1], event=Event.ASSIGNMENT).count(), 1)
        self.assertEqual(jobs.run(now=at(due - timedelta(days=1)))["reminders_1d"], 1)
        self.assertEqual(jobs.run(now=at(due))["reminders_1d"], 0)
        stats = jobs.run(now=at(due + timedelta(days=1)))
        self.assertEqual(stats["overdue"], 1)
        self.assertTrue(Notification.objects.filter(user=self.chairman, title__startswith="Просрочено поручение").exists())
        self.assertEqual(jobs.run(now=at(due + timedelta(days=2)))["overdue"], 0)
        a.refresh_from_db()
        self.assertTrue(a.reminded_7d and a.reminded_1d and a.overdue_notified)

    def test_draft_assignments_not_active(self):
        protocol = self.make_draft()
        draft_assignment = Assignment.objects.get(decision__protocol=protocol)
        with self.assertRaises(ValidationError):
            services.start_assignment(draft_assignment, self.responsible)
        stats = jobs.run(now=timezone.make_aware(datetime.combine(draft_assignment.due_date, time(8, 0))))
        self.assertEqual(stats["reminders_1d"], 1)  # напоминание — только по поручению подписанного протокола
        draft_assignment.refresh_from_db()
        self.assertFalse(draft_assignment.reminded_1d)


class AccessTests(ProtocolTestCase):
    def test_member_cannot_edit_outsider_forbidden(self):
        protocol = self.make_draft()
        detail = reverse("protocols:detail", args=[protocol.pk])
        item = protocol.items.first()
        self.login(self.members[0])
        self.assertEqual(self.client.get(detail).status_code, 200)
        self.assertEqual(self.client.get(reverse("protocols:edit", args=[protocol.pk])).status_code, 403)
        self.assertEqual(self.client.post(reverse("protocols:item_edit", args=[item.pk]), {"title": "x"}).status_code, 403)
        self.assertEqual(self.client.post(reverse("protocols:send_to_approval", args=[protocol.pk])).status_code, 403)
        self.assertEqual(self.client.get(reverse("protocols:preview_pdf", args=[protocol.pk])).status_code, 403)
        self.assertEqual(self.client.get(reverse("protocols:export_docx", args=[protocol.pk])).status_code, 403)

        self.login(self.outsider)
        self.assertEqual(self.client.get(detail).status_code, 403)
        self.assertEqual(self.client.get(reverse("protocols:list")).context["page"].paginator.count, 0)

        self.client.logout()
        self.assertEqual(self.client.get(detail).status_code, 302)

    def test_admin_can_manage(self):
        protocol = self.make_draft()
        self.login(self.admin)
        self.assertEqual(self.client.get(reverse("protocols:edit", args=[protocol.pk])).status_code, 200)

    def test_invited_sees_only_signed_protocol_of_meeting(self):
        signed = self.make_signed()
        draft = self.make_draft()
        guest = make_user()
        Invitation.objects.create(meeting=signed.meeting, user=guest, valid_until=timezone.localdate() + timedelta(days=5))
        Invitation.objects.create(meeting=draft.meeting, user=guest, valid_until=timezone.localdate() + timedelta(days=5))
        self.login(guest)
        page = self.client.get(reverse("protocols:detail", args=[signed.pk]))
        self.assertEqual(page.status_code, 200)
        self.assertNotContains(page, reverse("protocols:edit", args=[signed.pk]))
        self.assertNotContains(page, "Присутствие и кворум")
        self.assertEqual(self.client.get(reverse("protocols:download_signed", args=[signed.pk])).status_code, 200)
        self.assertEqual(self.client.get(reverse("protocols:detail", args=[draft.pk])).status_code, 403)
        listing = self.client.get(reverse("protocols:list"))
        self.assertEqual([p.pk for p in listing.context["page"]], [signed.pk])
        self.assertEqual(self.client.post(reverse("protocols:annul", args=[signed.pk]), {"reason": "x"}).status_code, 302)
        self.assertEqual(self.reload(signed).status, Protocol.Status.SIGNED)

    def test_list_filters(self):
        draft = self.make_draft()
        signed = self.make_signed()
        self.login(self.secretary)
        url = reverse("protocols:list")
        self.assertEqual([p.pk for p in self.client.get(url, {"status": "draft"}).context["page"]], [draft.pk])
        self.assertEqual([p.pk for p in self.client.get(url, {"status": "signed"}).context["page"]], [signed.pk])
        self.assertEqual(self.client.get(url, {"kind": "absentee"}).context["page"].paginator.count, 0)
        year = signed.protocol_date.year
        self.assertEqual(self.client.get(url, {"year": year}).context["page"].paginator.count, 2)
        self.assertEqual(self.client.get(url, {"q": signed.number}).context["page"].paginator.count, 1)
        self.assertEqual(self.client.get(url, {"commission": self.commission.pk}).context["page"].paginator.count, 2)
