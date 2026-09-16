from datetime import timedelta

from django.core.exceptions import ValidationError
from django.urls import reverse
from django.utils import timezone

from apps.committees.testing import make_commission
from apps.meetings.models import Transcript
from apps.voting.models import Vote, VotingSession

from .. import names, services
from ..models import Protocol
from .base import ProtocolTestCase


def closed_session(commission, question, users, agenda_item=None, meeting=None, outcome=VotingSession.Outcome.ADOPTED):
    s = VotingSession.objects.create(
        commission=commission, meeting=meeting, agenda_item=agenda_item, question=question,
        description=f"Проект решения: {question}", deadline=timezone.now() - timedelta(hours=1),
        status=VotingSession.Status.CLOSED, closed_at=timezone.now(), outcome=outcome,
        votes_for=3, votes_against=1, votes_abstain=1, quorum_needed=4,
    )
    s.eligible.set(users)
    for u, choice in zip(users, ["for", "for", "for", "against", "abstain"]):
        Vote.objects.create(session=s, user=u, choice=choice)
    return s


class DraftFromMeetingTests(ProtocolTestCase):
    def test_header_items_number_and_votes(self):
        meeting = self.make_meeting_with_agenda()
        first = meeting.agenda_items.first()
        users = [self.chairman, self.secretary, *self.members]
        session = closed_session(self.commission, "План", users, agenda_item=first, meeting=meeting)
        protocol = services.create_draft_from_meeting(meeting, self.secretary)

        year = timezone.localtime(meeting.starts_at).year
        self.assertEqual(protocol.number, f"{self.commission.short_name}-{year}/01")
        self.assertEqual(protocol.status, Protocol.Status.DRAFT)
        self.assertEqual(protocol.place, "Конференц-зал")
        self.assertEqual(protocol.format_text, "очное заседание")
        self.assertFalse(protocol.transcription_used)
        items = list(protocol.items.order_by("position"))
        self.assertEqual([i.title for i in items], ["Об утверждении плана работы", "Разное"])
        self.assertIn("Докладчик: " + self.secretary.short_name, items[0].heard)
        self.assertIn("Пояснение 1", items[0].heard)
        self.assertEqual(items[0].voting, session)
        self.assertIn("За — 3, против — 1, воздержались — 1", items[0].vote_summary)
        self.assertIn("решение принято", items[0].vote_summary)
        self.assertIn(session, protocol.votings.all())
        self.assertEqual(items[1].vote_summary, "")

        second = services.create_draft_from_meeting(meeting, self.secretary)
        self.assertEqual(second.number, f"{self.commission.short_name}-{year}/02")

    def test_transcript_split_by_items(self):
        meeting = self.make_meeting_with_agenda()
        text = (
            "Открытие заседания.\n"
            "Первый вопрос: об утверждении плана работы. Выступил докладчик, план поддержан.\n"
            "Переходим к вопросу разное. Вопросов нет."
        )
        transcript = Transcript.objects.create(meeting=meeting, status=Transcript.Status.DONE, text=text)
        protocol = services.create_draft_from_meeting(meeting, self.secretary, transcript)
        self.assertTrue(protocol.transcription_used)
        first, second = protocol.items.order_by("position")
        self.assertIn("план поддержан", first.discussed)
        self.assertIn("Открытие заседания", first.discussed)
        self.assertNotIn("Вопросов нет", first.discussed)
        self.assertIn("Вопросов нет", second.discussed)

    def test_transcript_not_split_goes_to_first_item(self):
        meeting = self.make_meeting_with_agenda()
        transcript = Transcript.objects.create(meeting=meeting, status=Transcript.Status.DONE, text="Просто текст обсуждения.")
        protocol = services.create_draft_from_meeting(meeting, self.secretary, transcript)
        first, second = protocol.items.order_by("position")
        self.assertIn(services.TRANSCRIPT_NOTE, first.discussed)
        self.assertIn("Просто текст обсуждения.", first.discussed)
        self.assertEqual(second.discussed, "")

    def test_flagged_names_exclude_system_users(self):
        commission, people = make_commission(members=2, handles_patient_cases=True)
        chairman = people["chairman"]
        chairman.last_name, chairman.first_name, chairman.middle_name = "Сериков", "Арман", "Болатович"
        chairman.save()
        meeting = self.make_meeting_with_agenda(commission=commission, titles=["Клинический случай"])
        item = meeting.agenda_items.first()
        item.description = (
            "Пациентка Иванова Мария Петровна, также осмотрен Ахметов Б.К. "
            "Доклад председателя Серикова Армана Болатовича и замечание Сериков А.Б."
        )
        item.save()
        protocol = services.create_draft_from_meeting(meeting, people["secretary"])
        flagged = protocol.items.first().flagged_names
        self.assertIn("Иванова Мария Петровна", flagged)
        self.assertIn("Ахметов Б.К.", flagged)
        self.assertFalse(any("Сериков" in f for f in flagged))
        self.assertTrue(services.requires_deidentification(protocol))

        with self.assertRaises(ValidationError):
            services.send_to_approval(protocol, people["secretary"])
        protocol.patient_ids_confirmed = True
        protocol.save()
        services.send_to_approval(protocol, people["secretary"])
        self.assertEqual(protocol.status, Protocol.Status.APPROVAL)

    def test_name_regex(self):
        found = names.find_person_names("Слушали Петрова Ивана Сергеевича и И.И. Смирнова. Центр Гематологии Астана.")
        self.assertIn("Петрова Ивана Сергеевича", found)
        self.assertIn("И.И. Смирнова", found)
        self.assertEqual(len(found), 2)

    def test_create_view_get_and_post(self):
        meeting = self.make_meeting_with_agenda()
        url = reverse("protocols:create_from_meeting", args=[meeting.pk])
        self.login(self.secretary)
        self.assertEqual(self.client.get(url).status_code, 200)
        response = self.client.post(url, {"transcript": ""})
        protocol = Protocol.objects.get(meeting=meeting)
        self.assertRedirects(response, reverse("protocols:detail", args=[protocol.pk]))
        self.login(self.members[0])
        self.assertEqual(self.client.get(url).status_code, 403)


class AbsenteeProtocolTests(ProtocolTestCase):
    def test_absentee_from_closed_sessions(self):
        users = [self.chairman, self.secretary, *self.members]
        s1 = closed_session(self.commission, "Вопрос А", users)
        s2 = closed_session(self.commission, "Вопрос Б", users[:3], outcome=VotingSession.Outcome.NO_QUORUM)
        protocol = services.create_absentee_protocol([s1, s2], self.secretary)
        self.assertEqual(protocol.kind, Protocol.Kind.ABSENTEE)
        self.assertEqual(protocol.protocol_date, timezone.localdate())
        self.assertEqual(protocol.format_text, "заочное голосование")
        self.assertEqual(set(protocol.votings.all()), {s1, s2})
        items = list(protocol.items.order_by("position"))
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0].title, "Вопрос А")
        self.assertEqual(items[0].resolved, "Решение принято")
        self.assertIn("кворум отсутствует", items[1].vote_summary)
        pdf = services.draft_pdf(protocol)
        self.assertTrue(pdf.startswith(b"%PDF"))

    def test_open_session_rejected(self):
        s = VotingSession.objects.create(
            commission=self.commission, question="Открытое", deadline=timezone.now() + timedelta(days=1),
            status=VotingSession.Status.OPEN,
        )
        with self.assertRaises(ValidationError):
            services.create_absentee_protocol([s], self.secretary)

    def test_absentee_view(self):
        s1 = closed_session(self.commission, "Вопрос В", [self.chairman, self.secretary])
        self.login(self.secretary)
        url = reverse("protocols:create_absentee", args=[self.commission.pk])
        self.assertContains(self.client.get(url), "Вопрос В")
        response = self.client.post(url, {"sessions": [s1.pk]})
        protocol = Protocol.objects.get(kind=Protocol.Kind.ABSENTEE)
        self.assertRedirects(response, reverse("protocols:detail", args=[protocol.pk]))
        # голосование уже в протоколе — больше не предлагается
        self.assertNotContains(self.client.get(url), "Вопрос В")


class EditingTests(ProtocolTestCase):
    def test_item_crud_and_reorder(self):
        protocol = self.make_draft()
        self.login(self.secretary)
        self.client.post(reverse("protocols:item_add", args=[protocol.pk]))
        self.assertEqual(protocol.items.count(), 3)
        new = protocol.items.get(position=3)
        response = self.client.post(reverse("protocols:item_edit", args=[new.pk]), {
            "title": "Третий вопрос", "heard": "Слушали Сидорова Петра Ивановича", "discussed": "", "resolved": "", "vote_summary": "",
        })
        self.assertEqual(response.status_code, 302)
        new.refresh_from_db()
        self.assertEqual(new.title, "Третий вопрос")
        self.assertEqual(new.flagged_names, ["Сидорова Петра Ивановича"])
        self.client.post(reverse("protocols:item_move", args=[new.pk]), {"direction": "up"})
        new.refresh_from_db()
        self.assertEqual(new.position, 2)
        self.client.post(reverse("protocols:item_delete", args=[new.pk]))
        self.assertEqual(list(protocol.items.values_list("position", flat=True).order_by("position")), [1, 2])

        item = protocol.items.get(position=1)
        self.client.post(reverse("protocols:decision_add", args=[item.pk]), {"number": "1.2", "text": "Ещё решение"})
        decision = item.decisions.get(number="1.2")
        response = self.client.post(reverse("protocols:assignment_add", args=[decision.pk]), {
            "text": "Сделать", "responsible_name": "Внешний Исполнитель", "due_date": "2030-01-01",
        })
        self.assertEqual(response.status_code, 302)
        self.assertEqual(decision.assignments.get().responsible_display, "Внешний Исполнитель")
        response = self.client.post(reverse("protocols:assignment_add", args=[decision.pk]), {"text": "Без ответственного", "due_date": "2030-01-01"})
        self.assertEqual(response.status_code, 200)  # ошибка формы

        self.assertEqual(self.client.get(reverse("protocols:detail", args=[protocol.pk])).status_code, 200)
        self.assertEqual(self.client.get(reverse("protocols:preview_pdf", args=[protocol.pk]))["Content-Type"], "application/pdf")
        docx = self.client.get(reverse("protocols:export_docx", args=[protocol.pk]))
        self.assertEqual(docx.status_code, 200)
        self.assertTrue(docx.content.startswith(b"PK"))

    def test_edit_forbidden_outside_draft(self):
        protocol = self.make_draft()
        services.send_to_approval(protocol, self.secretary)
        self.login(self.secretary)
        response = self.client.get(reverse("protocols:item_edit", args=[protocol.items.first().pk]))
        self.assertEqual(response.status_code, 403)

    def test_patient_id_field_validation(self):
        commission, people = make_commission(members=1, handles_patient_cases=True)
        meeting = self.make_meeting_with_agenda(commission=commission, titles=["Случай"])
        protocol = services.create_draft_from_meeting(meeting, people["secretary"])
        item = protocol.items.first()
        self.login(people["secretary"])
        response = self.client.post(reverse("protocols:item_edit", args=[item.pk]), {
            "title": "Случай", "patient_id": "Иванов Иван!", "heard": "", "discussed": "", "resolved": "", "vote_summary": "",
        })
        self.assertEqual(response.status_code, 200)
        response = self.client.post(reverse("protocols:item_edit", args=[item.pk]), {
            "title": "Случай", "patient_id": "MIS-12345", "heard": "", "discussed": "", "resolved": "", "vote_summary": "",
        })
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.reload(item).patient_id, "MIS-12345")
