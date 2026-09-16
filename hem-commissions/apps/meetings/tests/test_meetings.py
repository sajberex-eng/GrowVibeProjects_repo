from datetime import timedelta

from django.core import mail
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse
from django.utils import timezone

from apps.committees.models import AbsenceReason
from apps.committees.testing import make_commission, make_meeting, make_user
from apps.meetings import jobs, services
from apps.meetings.forms import AgendaItemForm, ProposalForm
from apps.meetings.models import (
    AgendaAcknowledgement, AgendaItem, AgendaProposal, Attendance, Invitation, Material, Meeting, MeetingReschedule,
    Transcript,
)
from apps.notifications.models import Event, Notification

from .base import MeetingTestCase


def dt_local(dt):
    return timezone.localtime(dt).strftime("%Y-%m-%dT%H:%M")


class CreateMeetingTests(MeetingTestCase):
    def test_create_via_view_notifies_composition(self):
        self.login(self.secretary)
        starts = timezone.now() + timedelta(days=10)
        resp = self.client.post(
            reverse("meetings:create") + f"?commission={self.commission.pk}",
            {"kind": "planned", "starts_at": dt_local(starts), "duration_minutes": 60, "format": "offline",
             "place": "Зал 1"},
        )
        self.assertEqual(resp.status_code, 302, getattr(resp, "context", None) and resp.context["form"].errors)
        meeting = Meeting.objects.get(commission=self.commission)
        self.assertEqual(meeting.created_by, self.secretary)
        notes = Notification.objects.filter(event=Event.MEETING_SCHEDULED)
        self.assertEqual(notes.count(), 5)  # председатель, секретарь, 3 члена
        self.assertFalse(notes.filter(user=self.outsider).exists())
        self.assertTrue(len(mail.outbox) >= 5)

    def test_create_with_commission_choice(self):
        self.login(self.secretary)
        starts = timezone.now() + timedelta(days=3)
        resp = self.client.post(reverse("meetings:create"), {
            "commission": self.commission.pk, "kind": "extra", "starts_at": dt_local(starts),
            "duration_minutes": 60, "format": "online", "video_link": "https://meet.example.kz/abc",
        })
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(Meeting.objects.get().kind, Meeting.Kind.EXTRA)

    def test_online_requires_link(self):
        self.login(self.secretary)
        resp = self.client.post(reverse("meetings:create") + f"?commission={self.commission.pk}", {
            "kind": "planned", "starts_at": dt_local(timezone.now() + timedelta(days=3)),
            "duration_minutes": 60, "format": "online",
        })
        self.assertEqual(resp.status_code, 200)
        self.assertIn("video_link", resp.context["form"].errors)

    def test_member_cannot_create(self):
        self.login(self.member)
        resp = self.client.get(reverse("meetings:create") + f"?commission={self.commission.pk}")
        self.assertEqual(resp.status_code, 403)

    def test_invited_notified_on_create(self):
        meeting = Meeting(commission=self.commission, starts_at=timezone.now() + timedelta(days=5))
        meeting.save()
        guest = make_user()
        Invitation.objects.create(meeting=meeting, user=guest, valid_until=timezone.localdate() + timedelta(days=30))
        self.assertIn(guest, services.meeting_recipients(meeting))


class RescheduleCancelTests(MeetingTestCase):
    def test_reschedule_keeps_original_and_logs(self):
        meeting = self.meeting(days=5)
        original = meeting.starts_at
        meeting.reminder_3d_sent = True
        meeting.save()
        self.login(self.secretary)
        first = timezone.now() + timedelta(days=8)
        resp = self.client.post(reverse("meetings:reschedule", args=[meeting.pk]),
                                {"new_starts_at": dt_local(first), "reason": "Командировка председателя"})
        self.assertEqual(resp.status_code, 302)
        second = timezone.now() + timedelta(days=12)
        services.reschedule_meeting(meeting, second.replace(second=0, microsecond=0), "ещё раз", self.secretary)
        meeting.refresh_from_db()
        self.assertEqual(meeting.original_starts_at, original)
        self.assertEqual(meeting.status, Meeting.Status.POSTPONED)
        self.assertTrue(meeting.is_upcoming)
        self.assertFalse(meeting.reminder_3d_sent)
        self.assertEqual(MeetingReschedule.objects.filter(meeting=meeting).count(), 2)
        log = MeetingReschedule.objects.filter(meeting=meeting).order_by("changed_at", "id").first()
        self.assertEqual(log.old_starts_at, original)
        self.assertEqual(log.reason, "Командировка председателя")
        self.assertEqual(Notification.objects.filter(event=Event.MEETING_SCHEDULED).count(), 10)

    def test_cancel(self):
        meeting = self.meeting()
        self.login(self.chairman)
        resp = self.client.post(reverse("meetings:cancel", args=[meeting.pk]), {"reason": "Нет кворума"})
        self.assertEqual(resp.status_code, 302)
        meeting.refresh_from_db()
        self.assertEqual(meeting.status, Meeting.Status.CANCELLED)
        self.assertEqual(meeting.cancel_reason, "Нет кворума")
        self.assertEqual(Notification.objects.filter(event=Event.MEETING_SCHEDULED, user=self.member).count(), 1)

    def test_member_cannot_reschedule(self):
        meeting = self.meeting()
        self.login(self.member)
        resp = self.client.post(reverse("meetings:reschedule", args=[meeting.pk]),
                                {"new_starts_at": dt_local(timezone.now() + timedelta(days=9)), "reason": "x"})
        self.assertEqual(resp.status_code, 403)

    def test_mark_held(self):
        meeting = self.meeting(days=-1)
        self.login(self.secretary)
        self.client.post(reverse("meetings:mark_held", args=[meeting.pk]))
        meeting.refresh_from_db()
        self.assertEqual(meeting.status, Meeting.Status.HELD)
        future = self.meeting(days=5)
        self.client.post(reverse("meetings:mark_held", args=[future.pk]))
        future.refresh_from_db()
        self.assertEqual(future.status, Meeting.Status.PLANNED)


class QuorumTests(MeetingTestCase):
    members = 5  # + председатель + секретарь = 7

    def _mark(self, meeting, users, status=Attendance.Status.PRESENT):
        for u in users:
            Attendance.objects.create(meeting=meeting, user=u, status=status)

    def test_four_of_seven_reached(self):
        meeting = self.meeting()
        self._mark(meeting, [self.chairman, self.secretary, self.plain[0]])
        self._mark(meeting, [self.plain[1]], Attendance.Status.ONLINE)
        q = meeting.quorum()
        self.assertEqual((q["present"], q["total"], q["needed"]), (4, 7, 4))
        self.assertTrue(q["reached"])

    def test_three_of_seven_not_reached(self):
        meeting = self.meeting()
        self._mark(meeting, [self.chairman, self.secretary, self.plain[0]])
        self._mark(meeting, [self.plain[1]], Attendance.Status.ABSENT_EXCUSED)
        q = meeting.quorum()
        self.assertEqual(q["present"], 3)
        self.assertFalse(q["reached"])

    def test_invited_not_counted_and_attendance_view(self):
        meeting = self.meeting(days=0)
        guest = make_user()
        Invitation.objects.create(meeting=meeting, user=guest, valid_until=timezone.localdate())
        reason = AbsenceReason.objects.create(name="Отпуск")
        self.login(self.secretary)
        url = reverse("meetings:attendance", args=[meeting.pk])
        self.assertEqual(self.client.get(url).status_code, 200)
        data = {f"status_{u.pk}": "present" for u in [self.chairman, self.secretary, *self.plain[:2], guest]}
        data[f"status_{self.plain[2].pk}"] = "absent_excused"
        data[f"reason_{self.plain[2].pk}"] = reason.pk
        resp = self.client.post(url, data, follow=True)
        self.assertEqual(resp.status_code, 200)
        q = meeting.quorum()
        self.assertEqual(q["present"], 4)
        self.assertTrue(q["reached"])
        self.assertTrue(Attendance.objects.get(meeting=meeting, user=guest).is_invited)
        self.assertEqual(Attendance.objects.get(meeting=meeting, user=self.plain[2]).reason, reason)
        # снятие отметки
        data[f"status_{self.chairman.pk}"] = ""
        self.client.post(url, data)
        self.assertEqual(meeting.quorum()["present"], 3)

    def test_quorum_not_default(self):
        self.commission.meeting_quorum_numerator = 2
        self.commission.meeting_quorum_denominator = 3
        self.commission.meeting_quorum_strict = False
        self.commission.save()
        meeting = self.meeting()
        self.assertEqual(meeting.quorum()["needed"], 5)


class AgendaTests(MeetingTestCase):
    def test_approval_only_by_chairman(self):
        meeting = self.meeting()
        AgendaItem.objects.create(meeting=meeting, title="Вопрос 1")
        guest = make_user()
        Invitation.objects.create(meeting=meeting, user=guest, valid_until=timezone.localdate() + timedelta(days=30))
        url = reverse("meetings:agenda_approve", args=[meeting.pk])
        for user in (self.secretary, self.member, guest):
            self.login(user)
            self.assertEqual(self.client.post(url).status_code, 403)
        meeting.refresh_from_db()
        self.assertEqual(meeting.agenda_status, Meeting.AgendaStatus.DRAFT)

        self.login(self.secretary)
        self.client.post(reverse("meetings:agenda_submit", args=[meeting.pk]))
        meeting.refresh_from_db()
        self.assertEqual(meeting.agenda_status, Meeting.AgendaStatus.SUBMITTED)
        self.assertTrue(Notification.objects.filter(user=self.chairman, event=Event.AGENDA_APPROVAL).exists())

        self.login(self.chairman)
        self.assertEqual(self.client.post(url).status_code, 302)
        meeting.refresh_from_db()
        self.assertEqual(meeting.agenda_status, Meeting.AgendaStatus.APPROVED)
        self.assertEqual(meeting.agenda_approved_by, self.chairman)
        recipients = set(
            Notification.objects.filter(event=Event.AGENDA_APPROVAL, title__startswith="Утверждена").values_list("user_id", flat=True)
        )
        self.assertEqual(recipients, {self.chairman.pk, self.secretary.pk, guest.pk, *[u.pk for u in self.plain]})
        self.assertFalse(meeting.is_open_for_proposals)

    def test_acknowledge_and_suggestion(self):
        meeting = self.meeting()
        AgendaItem.objects.create(meeting=meeting, title="Вопрос 1")
        url = reverse("meetings:acknowledge", args=[meeting.pk])
        self.login(self.member)
        self.client.post(url, {"status": "ack"})
        self.assertFalse(AgendaAcknowledgement.objects.exists())  # повестка ещё не утверждена
        services.approve_agenda(meeting, self.chairman)
        self.client.post(url, {"status": "ack"})
        self.assertEqual(AgendaAcknowledgement.objects.get(user=self.member).status, "ack")
        self.login(self.plain[1])
        self.client.post(url, {"status": "suggestion", "text": "Добавить вопрос о закупках"})
        self.assertTrue(Notification.objects.filter(user=self.secretary, event=Event.AGENDA_PROPOSAL).exists())
        self.login(self.secretary)
        resp = self.client.get(reverse("meetings:detail", args=[meeting.pk]) + "?tab=participants")
        self.assertContains(resp, "Ознакомились: 2 из 5")
        self.assertContains(resp, "Добавить вопрос о закупках")

    def test_items_add_move_delete_and_reapproval(self):
        meeting = self.meeting()
        self.login(self.secretary)
        for title in ("Первый", "Второй"):
            resp = self.client.post(reverse("meetings:item_add", args=[meeting.pk]),
                                    {"title": title, "duration_minutes": 10})
            self.assertEqual(resp.status_code, 302)
        first, second = AgendaItem.objects.filter(meeting=meeting).order_by("position")
        self.client.post(reverse("meetings:item_move", args=[second.pk, "up"]))
        self.assertEqual(
            list(AgendaItem.objects.filter(meeting=meeting).order_by("position").values_list("title", flat=True)),
            ["Второй", "Первый"],
        )
        services.approve_agenda(meeting, self.chairman)
        self.client.post(reverse("meetings:item_delete", args=[first.pk]))
        meeting.refresh_from_db()
        self.assertEqual(meeting.agenda_status, Meeting.AgendaStatus.DRAFT)
        self.assertEqual(AgendaItem.objects.get(meeting=meeting).position, 1)


class ProposalTests(MeetingTestCase):
    def test_member_proposes_and_secretary_accepts(self):
        meeting = self.meeting()
        self.login(self.member)
        upload = SimpleUploadedFile("справка.pdf", b"%PDF-1.4 test", content_type="application/pdf")
        resp = self.client.post(reverse("meetings:proposal_create", args=[meeting.pk]), {
            "title": "Закупка реагентов", "description": "Обоснование", "speaker_name": "Иванов И.И.",
            "requires_vote": "on", "file": upload,
        })
        self.assertEqual(resp.status_code, 302, resp.context and resp.context["form"].errors)
        proposal = AgendaProposal.objects.get()
        self.assertEqual(proposal.author, self.member)
        self.assertEqual(proposal.file_name, "справка.pdf")
        self.assertTrue(Notification.objects.filter(user=self.secretary, event=Event.AGENDA_PROPOSAL).exists())

        # автор видит файл, посторонний член — нет
        self.assertEqual(self.client.get(reverse("meetings:proposal_file", args=[proposal.pk])).status_code, 200)
        self.login(self.plain[1])
        self.assertEqual(self.client.get(reverse("meetings:proposal_file", args=[proposal.pk])).status_code, 403)
        self.assertEqual(self.client.post(reverse("meetings:proposal_accept", args=[proposal.pk])).status_code, 403)

        self.login(self.secretary)
        resp = self.client.post(reverse("meetings:proposal_accept", args=[proposal.pk]))
        self.assertEqual(resp.status_code, 302)
        proposal.refresh_from_db()
        self.assertEqual(proposal.status, AgendaProposal.Status.ACCEPTED)
        item = AgendaItem.objects.get(meeting=meeting)
        self.assertEqual(item.title, "Закупка реагентов")
        self.assertTrue(item.requires_vote)
        self.assertEqual(item.proposal, proposal)
        material = Material.objects.get(agenda_item=item)
        self.assertEqual(material.file_name, "справка.pdf")
        with material.file.open("rb") as fh:
            self.assertEqual(fh.read(), b"%PDF-1.4 test")
        self.assertTrue(
            Notification.objects.filter(user=self.member, event=Event.AGENDA_PROPOSAL, title__icontains="включено").exists()
        )

    def test_reject_with_reply(self):
        meeting = self.meeting()
        proposal = AgendaProposal.objects.create(meeting=meeting, author=self.member, title="Вопрос")
        self.login(self.secretary)
        resp = self.client.post(reverse("meetings:proposal_reject", args=[proposal.pk]), {"response": "Не по профилю"})
        self.assertEqual(resp.status_code, 302)
        proposal.refresh_from_db()
        self.assertEqual(proposal.status, AgendaProposal.Status.REJECTED)
        self.assertEqual(proposal.response, "Не по профилю")
        note = Notification.objects.get(user=self.member, event=Event.AGENDA_PROPOSAL)
        self.assertIn("Не по профилю", note.body)
        self.assertFalse(AgendaItem.objects.exists())

    def test_closed_after_deadline_and_for_outsiders(self):
        meeting = self.meeting(proposals_deadline=timezone.now() - timedelta(hours=1))
        self.login(self.member)
        self.assertEqual(self.client.get(reverse("meetings:proposal_create", args=[meeting.pk])).status_code, 403)
        open_meeting = self.meeting()
        self.login(self.outsider)
        self.assertEqual(self.client.get(reverse("meetings:proposal_create", args=[open_meeting.pk])).status_code, 403)


class PatientIdTests(MeetingTestCase):
    commission_kwargs = {"handles_patient_cases": True, "short_name": "КИЛИ"}

    def _form(self, commission, value):
        return AgendaItemForm({"title": "Клинический случай", "duration_minutes": 10, "patient_id": value},
                              commission=commission)

    def test_valid_and_invalid_ids(self):
        self.assertTrue(self._form(self.commission, "MIS-104233").is_valid())
        self.assertTrue(self._form(self.commission, "").is_valid())
        for bad in ("Иванов Иван", "Иванов", "870101300123", "id;drop", "a" * 41):
            form = self._form(self.commission, bad)
            self.assertFalse(form.is_valid(), bad)
            self.assertIn("patient_id", form.errors)
        self.assertIn("МИС", self._form(self.commission, "").fields["patient_id"].help_text)

    def test_hidden_for_other_commissions(self):
        vkk, _ = make_commission(short_name="ВКК")
        form = self._form(vkk, "Иванов")
        self.assertNotIn("patient_id", form.fields)
        self.assertTrue(form.is_valid())
        self.assertNotIn("patient_id", ProposalForm(commission=vkk).fields)
        self.assertIn("patient_id", ProposalForm(commission=self.commission).fields)

    def test_view_saves_patient_id(self):
        meeting = self.meeting()
        self.login(self.secretary)
        resp = self.client.post(reverse("meetings:item_add", args=[meeting.pk]),
                                {"title": "Случай", "duration_minutes": 15, "patient_id": "Петров П.П."})
        self.assertEqual(resp.status_code, 200)
        self.client.post(reverse("meetings:item_add", args=[meeting.pk]),
                         {"title": "Случай", "duration_minutes": 15, "patient_id": "12345"})
        self.assertEqual(AgendaItem.objects.get(meeting=meeting).patient_id, "12345")


class InvitedAccessTests(MeetingTestCase):
    def test_invited_sees_only_that_meeting(self):
        meeting = self.meeting()
        other = self.meeting(days=9)
        guest = make_user()
        Invitation.objects.create(meeting=meeting, user=guest, valid_until=timezone.localdate() + timedelta(days=3))
        self.login(guest)
        resp = self.client.get(reverse("meetings:detail", args=[meeting.pk]))
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.context["invited_only"])
        self.assertEqual({k for k, _ in resp.context["tabs"]}, {"agenda", "materials", "protocols"})
        resp = self.client.get(reverse("meetings:detail", args=[meeting.pk]) + "?tab=participants")
        self.assertEqual(resp.context["tab"], "agenda")
        self.assertEqual(self.client.get(reverse("meetings:detail", args=[other.pk])).status_code, 403)
        self.assertEqual(self.client.get(reverse("meetings:ics", args=[other.pk])).status_code, 403)
        self.assertEqual(self.client.get(reverse("meetings:edit", args=[meeting.pk])).status_code, 403)
        # календарь и список — только приглашённое заседание
        resp = self.client.get(reverse("meetings:list") + "?period=all")
        self.assertEqual([m.pk for m in resp.context["page"]], [meeting.pk])
        resp = self.client.get(reverse("meetings:calendar") + "?view=year&year=" + str(timezone.localtime(meeting.starts_at).year))
        ids = {m.pk for month in resp.context["months"] for m in month["meetings"]}
        self.assertIn(meeting.pk, ids)
        self.assertNotIn(other.pk, ids)

    def test_expired_invitation(self):
        meeting = self.meeting()
        guest = make_user()
        Invitation.objects.create(meeting=meeting, user=guest, valid_until=timezone.localdate() - timedelta(days=1))
        self.login(guest)
        self.assertEqual(self.client.get(reverse("meetings:detail", args=[meeting.pk])).status_code, 403)

    def test_invitation_add_and_remove(self):
        meeting = self.meeting()
        guest = make_user()
        self.login(self.secretary)
        resp = self.client.post(reverse("meetings:invitation_add", args=[meeting.pk]), {
            "user": guest.pk, "valid_until": (timezone.localdate() + timedelta(days=20)).isoformat(), "note": "эксперт",
        })
        self.assertEqual(resp.status_code, 302)
        inv = Invitation.objects.get(meeting=meeting)
        self.assertEqual(inv.created_by, self.secretary)
        self.assertTrue(Notification.objects.filter(user=guest, event=Event.MEETING_SCHEDULED).exists())
        self.client.post(reverse("meetings:invitation_delete", args=[inv.pk]))
        self.assertFalse(Invitation.objects.exists())


class MaterialTests(MeetingTestCase):
    def test_upload_and_download_permissions(self):
        meeting = self.meeting()
        self.login(self.secretary)
        resp = self.client.post(reverse("meetings:material_add", args=[meeting.pk]) + "?type=file", {
            "file": SimpleUploadedFile("Отчёт.docx", b"docx-bytes"),
        })
        self.assertEqual(resp.status_code, 302)
        material = Material.objects.get()
        self.assertEqual(material.title, "Отчёт.docx")
        self.assertEqual(material.file_size, 10)
        self.assertNotIn("Отчёт", material.file.name)
        self.assertEqual(Notification.objects.filter(event=Event.MATERIALS_ADDED).count(), 5)
        url = reverse("meetings:material_download", args=[material.pk])

        self.login(self.member)
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(b"".join(resp.streaming_content), b"docx-bytes")
        self.login(self.outsider)
        self.assertEqual(self.client.get(url).status_code, 403)
        guest = make_user()
        Invitation.objects.create(meeting=meeting, user=guest, valid_until=timezone.localdate())
        self.login(guest)
        self.assertEqual(self.client.get(url).status_code, 200)
        self.assertEqual(self.client.post(reverse("meetings:material_delete", args=[material.pk])).status_code, 403)

    def test_bad_extension_rejected(self):
        meeting = self.meeting()
        self.login(self.secretary)
        resp = self.client.post(reverse("meetings:material_add", args=[meeting.pk]) + "?type=file", {
            "file": SimpleUploadedFile("run.exe", b"MZ"),
        })
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(Material.objects.exists())

    def test_audio_link_and_transcription(self):
        meeting = self.meeting(days=-1)
        self.login(self.secretary)
        resp = self.client.post(reverse("meetings:material_add", args=[meeting.pk]) + "?type=audio", {
            "kind": "audio", "title": "Запись", "url": "https://storage.example.kz/rec.mp3", "silent": "1",
        })
        self.assertEqual(resp.status_code, 302)
        audio = Material.objects.get(kind=Material.Kind.AUDIO)
        self.assertFalse(audio.file)
        self.assertFalse(Notification.objects.filter(event=Event.MATERIALS_ADDED).exists())
        resp = self.client.post(reverse("meetings:transcribe", args=[audio.pk]), follow=True)
        self.assertContains(resp, "не настроен")
        self.assertFalse(Transcript.objects.exists())
        resp = self.client.post(reverse("meetings:transcript_manual", args=[meeting.pk]), {
            "audio": audio.pk, "text": "[00:00:05] Председатель: открываю заседание",
        })
        self.assertEqual(resp.status_code, 302)
        t = Transcript.objects.get()
        self.assertEqual(t.status, Transcript.Status.MANUAL)
        self.client.post(reverse("meetings:transcript_edit", args=[t.pk]), {"audio": audio.pk, "text": "исправлено"})
        t.refresh_from_db()
        self.assertEqual(t.text, "исправлено")
        self.assertEqual(self.client.get(reverse("meetings:transcript_view", args=[t.pk])).status_code, 200)
        self.login(self.outsider)
        self.assertEqual(self.client.get(reverse("meetings:transcript_view", args=[t.pk])).status_code, 403)

    def test_transcription_backend(self):
        from unittest import mock

        from apps.meetings import transcription

        meeting = self.meeting(days=-1)
        audio = Material.objects.create(meeting=meeting, kind="audio", title="Запись", url="https://x.kz/a.mp3")
        with self.settings(TRANSCRIPTION_BACKEND="http", TRANSCRIPTION_URL="http://asr.local/"):
            with mock.patch.object(transcription, "transcribe", return_value="[00:00] текст"):
                t = transcription.run_for_material(audio, self.secretary)
        self.assertEqual(t.status, Transcript.Status.DONE)
        self.assertEqual(t.text, "[00:00] текст")
        with self.assertRaises(transcription.TranscriptionNotConfigured):
            transcription.transcribe("https://x.kz/a.mp3")


class IcalTests(MeetingTestCase):
    def test_meeting_ics(self):
        meeting = self.meeting(place="Зал; корпус 2")
        self.login(self.member)
        resp = self.client.get(reverse("meetings:ics", args=[meeting.pk]))
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp["Content-Type"].startswith("text/calendar"))
        body = resp.content.decode()
        self.assertIn("BEGIN:VCALENDAR", body)
        self.assertIn("BEGIN:VEVENT", body)
        self.assertIn(f"UID:meeting-{meeting.pk}@", body)
        self.assertIn("LOCATION:Зал\\; корпус 2", body)
        self.assertIn("\r\n", body)
        for line in body.split("\r\n"):
            self.assertLessEqual(len(line.encode()), 75)

    def test_commission_and_personal_ics(self):
        m1 = self.meeting()
        other_commission, _ = make_commission()
        m2 = make_meeting(other_commission)
        self.login(self.member)
        year = timezone.localtime(m1.starts_at).year
        resp = self.client.get(reverse("meetings:commission_ics", args=[self.commission.pk]) + f"?year={year}")
        self.assertEqual(resp.content.decode().count("BEGIN:VEVENT"), 1)
        self.assertEqual(
            self.client.get(reverse("meetings:commission_ics", args=[other_commission.pk])).status_code, 403
        )
        body = self.client.get(reverse("meetings:my_ics")).content.decode()
        self.assertIn(f"meeting-{m1.pk}@", body)
        self.assertNotIn(f"meeting-{m2.pk}@", body)
        self.client.logout()
        self.assertEqual(self.client.get(reverse("meetings:my_ics")).status_code, 302)

    def test_cancelled_status(self):
        meeting = self.meeting()
        services.cancel_meeting(meeting, "причина", self.secretary)
        from apps.meetings.ical import build_calendar

        self.assertIn("STATUS:CANCELLED", build_calendar([meeting]))


class ControlItemTests(MeetingTestCase):
    def _assignment(self, text, due, status="assigned"):
        from apps.protocols.models import Assignment, Decision, Protocol, ProtocolItem

        protocol = Protocol.objects.create(commission=self.commission, number="7", protocol_date=timezone.localdate() - timedelta(days=30))
        item = ProtocolItem.objects.create(protocol=protocol, title="Вопрос")
        decision = Decision.objects.create(protocol=protocol, item=item, text="Решение")
        return Assignment.objects.create(decision=decision, text=text, due_date=due, status=status,
                                         responsible=self.member)

    def test_no_assignments_no_item(self):
        meeting = services.create_meeting(
            Meeting(commission=self.commission, starts_at=timezone.now() + timedelta(days=5)), self.secretary
        )
        self.assertFalse(meeting.agenda_items.exists())

    def test_control_item_lists_open_assignments(self):
        today = timezone.localdate()
        self._assignment("Подготовить СОП по трансфузиям", today - timedelta(days=2))
        self._assignment("Обновить формуляр", today + timedelta(days=10), status="in_progress")
        self._assignment("Закрытое поручение", today, status="done")
        meeting = Meeting(commission=self.commission, starts_at=timezone.now() + timedelta(days=5))
        meeting.save()
        AgendaItem.objects.create(meeting=meeting, title="Обычный вопрос", position=1)
        services.ensure_control_item(meeting)
        items = list(meeting.agenda_items.order_by("position"))
        self.assertTrue(items[0].is_control_item)
        self.assertEqual(items[0].position, 1)
        self.assertEqual(items[1].title, "Обычный вопрос")
        self.assertEqual(items[1].position, 2)
        text = items[0].description
        self.assertIn("Подготовить СОП по трансфузиям", text)
        self.assertIn("Обновить формуляр", text)
        self.assertNotIn("Закрытое поручение", text)
        self.assertIn("ПРОСРОЧЕНО", text)
        self.assertIn(self.member.short_name, text)
        self.assertIn((today - timedelta(days=2)).strftime("%d.%m.%Y"), text)
        # повторный вызов не создаёт дубль
        self._assignment("Новое поручение", today + timedelta(days=3))
        self.login(self.secretary)
        self.client.post(reverse("meetings:control_refresh", args=[meeting.pk]))
        self.assertEqual(meeting.agenda_items.filter(is_control_item=True).count(), 1)
        self.assertIn("Новое поручение", meeting.agenda_items.get(is_control_item=True).description)
        # control item cannot move
        control = meeting.agenda_items.get(is_control_item=True)
        self.assertFalse(services.move_item(meeting.agenda_items.get(title="Обычный вопрос"), "up"))
        self.assertFalse(services.move_item(control, "down"))

    def test_created_on_meeting_create(self):
        self._assignment("Поручение", timezone.localdate() + timedelta(days=3))
        meeting = services.create_meeting(
            Meeting(commission=self.commission, starts_at=timezone.now() + timedelta(days=5)), self.secretary
        )
        self.assertTrue(meeting.agenda_items.filter(is_control_item=True, position=1).exists())


class JobsTests(MeetingTestCase):
    def test_reminders_sent_once(self):
        now = timezone.now()
        soon = self.meeting(starts_at=now + timedelta(days=2, hours=12))
        far = self.meeting(starts_at=now + timedelta(days=10))
        cancelled = self.meeting(starts_at=now + timedelta(days=2), status=Meeting.Status.CANCELLED)
        guest = make_user()
        Invitation.objects.create(meeting=soon, user=guest, valid_until=timezone.localdate() + timedelta(days=5))
        expired_guest = make_user()
        Invitation.objects.create(meeting=soon, user=expired_guest, valid_until=timezone.localdate() - timedelta(days=1))

        result = jobs.run(now)
        self.assertEqual(result["reminders_3d"], 1)
        self.assertEqual(result["reminders_1d"], 0)
        reminders = Notification.objects.filter(event=Event.MEETING_REMINDER)
        self.assertEqual(reminders.count(), 6)
        self.assertTrue(reminders.filter(user=guest).exists())
        self.assertFalse(reminders.filter(user=expired_guest).exists())
        self.assertEqual(jobs.run(now)["reminders_3d"], 0)
        self.assertEqual(reminders.count(), 6)

        later = now + timedelta(days=1, hours=13)
        result = jobs.run(later)
        self.assertEqual(result["reminders_1d"], 1)
        self.assertEqual(jobs.run(later + timedelta(hours=1))["reminders_1d"], 0)
        self.assertEqual(Notification.objects.filter(event=Event.MEETING_REMINDER).count(), 12)
        soon.refresh_from_db()
        far.refresh_from_db()
        cancelled.refresh_from_db()
        self.assertTrue(soon.reminder_1d_sent and soon.reminder_3d_sent)
        self.assertFalse(far.reminder_3d_sent)
        self.assertFalse(cancelled.reminder_3d_sent)

    def test_late_created_meeting_gets_only_1d(self):
        now = timezone.now()
        m = self.meeting(starts_at=now + timedelta(hours=20))
        result = jobs.run(now)
        self.assertEqual((result["reminders_3d"], result["reminders_1d"]), (0, 1))
        self.assertEqual(jobs.run(now), {"reminders_3d": 0, "reminders_1d": 0, "control_items_refreshed": 0})
        m.refresh_from_db()
        self.assertTrue(m.reminder_3d_sent)


class PagesRenderTests(MeetingTestCase):
    commission_kwargs = {"handles_patient_cases": True}

    def test_pages_render(self):
        meeting = self.meeting()
        item = AgendaItem.objects.create(meeting=meeting, title="Вопрос", requires_vote=True, patient_id="MIS-1")
        audio = Material.objects.create(meeting=meeting, kind="audio", title="Запись", url="https://x.kz/a.mp3")
        Transcript.objects.create(meeting=meeting, audio=audio, status="manual", text="текст")
        AgendaProposal.objects.create(meeting=meeting, author=self.member, title="Предложение")
        self.login(self.secretary)
        urls = [
            reverse("meetings:calendar"),
            reverse("meetings:calendar") + "?view=year",
            reverse("meetings:calendar") + f"?commission={self.commission.pk}&month=2026-02",
            reverse("meetings:calendar") + "?month=bad&year=bad&view=year",
            reverse("meetings:list"),
            reverse("meetings:list") + "?period=past&status=held&year=2026",
            reverse("meetings:create"),
            reverse("meetings:create") + f"?commission={self.commission.pk}&date=2026-12-01",
            reverse("meetings:edit", args=[meeting.pk]),
            reverse("meetings:reschedule", args=[meeting.pk]),
            reverse("meetings:cancel", args=[meeting.pk]),
            reverse("meetings:schedule", args=[self.commission.pk]),
            reverse("meetings:item_add", args=[meeting.pk]),
            reverse("meetings:item_edit", args=[item.pk]),
            reverse("meetings:material_add", args=[meeting.pk]),
            reverse("meetings:material_add", args=[meeting.pk]) + "?type=audio",
            reverse("meetings:attendance", args=[meeting.pk]),
            reverse("meetings:invitation_add", args=[meeting.pk]),
            reverse("meetings:transcript_manual", args=[meeting.pk]),
            reverse("meetings:proposal_reject", args=[AgendaProposal.objects.get().pk]),
        ]
        urls += [reverse("meetings:detail", args=[meeting.pk]) + f"?tab={t}" for t in
                 ("agenda", "materials", "participants", "proposals", "records", "protocols")]
        for url in urls:
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 200)
        resp = self.client.get(reverse("meetings:detail", args=[meeting.pk]))
        self.assertContains(resp, "MIS-1")
        self.assertNotContains(resp, "Утвердить повестку")  # утверждает только председатель
        self.assertContains(resp, "Открыть голосование") if resp.context["has_vote_create_url"] else None
        self.login(self.chairman)
        self.assertContains(self.client.get(reverse("meetings:detail", args=[meeting.pk])), "Утвердить повестку")
        self.login(self.member)
        resp = self.client.get(reverse("meetings:detail", args=[meeting.pk]))
        self.assertNotContains(resp, "Утвердить повестку")
        self.assertContains(resp, "Предложить вопрос")
        self.assertEqual(self.client.get(reverse("meetings:proposal_create", args=[meeting.pk])).status_code, 200)

    def test_agenda_docx(self):
        meeting = self.meeting(place="Зал")
        AgendaItem.objects.create(meeting=meeting, title="Вопрос о закупках", speaker=self.member, patient_id="MIS-7")
        self.login(self.member)
        resp = self.client.get(reverse("meetings:agenda_docx", args=[meeting.pk]))
        self.assertEqual(resp.status_code, 200)
        self.assertIn("wordprocessingml", resp["Content-Type"])
        import io

        from docx import Document

        doc = Document(io.BytesIO(resp.content))
        text = "\n".join(p.text for p in doc.paragraphs)
        self.assertIn("ПОВЕСТКА", text)
        self.assertIn("1. Вопрос о закупках", text)
        self.assertIn(self.member.short_name, text)
        self.assertIn("MIS-7", text)
        self.login(self.outsider)
        self.assertEqual(self.client.get(reverse("meetings:agenda_docx", args=[meeting.pk])).status_code, 403)
