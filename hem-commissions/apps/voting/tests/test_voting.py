import sys
import types
from datetime import timedelta
from unittest import mock

from django.core import mail
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from apps.committees.testing import make_commission, make_meeting, make_user
from apps.meetings.models import AgendaItem
from apps.notifications.models import Event, Notification
from apps.voting import jobs, services
from apps.voting.models import Vote, VotingSession

O = VotingSession.Outcome


class QuorumMathTests(TestCase):
    def test_two_thirds_quorum(self):
        commission, _ = make_commission(members=1)
        self.assertEqual(commission.vote_quorum_needed(7), 5)
        self.assertEqual(commission.vote_quorum_needed(6), 4)
        self.assertEqual(commission.vote_quorum_needed(9), 6)
        self.assertEqual(commission.vote_quorum_needed(3), 2)
        self.assertEqual(commission.vote_quorum_needed(1), 1)

    def test_outcome_rules(self):
        self.assertEqual(services.compute_outcome(7, 5, 3, 1, 0), O.NO_QUORUM)
        self.assertEqual(services.compute_outcome(7, 5, 3, 2, 0), O.ADOPTED)
        self.assertEqual(services.compute_outcome(7, 5, 4, 1, 1), O.ADOPTED)
        self.assertEqual(services.compute_outcome(7, 5, 3, 3, 0), O.REJECTED)  # ровно половина — не принято
        self.assertEqual(services.compute_outcome(7, 5, 2, 1, 2), O.REJECTED)  # воздержавшиеся учитываются
        self.assertEqual(services.compute_outcome(0, 0, 0, 0, 0), O.NO_QUORUM)

    def test_half_is_not_enough(self):
        # 6 проголосовавших, 3 «за» — не более половины
        self.assertEqual(services.compute_outcome(7, 5, 3, 1, 2), O.REJECTED)


class VotingBase(TestCase):
    def setUp(self):
        # председатель + секретарь + 5 членов = 7 голосующих, кворум 5
        self.commission, self.people = make_commission(members=5)
        self.chairman = self.people["chairman"]
        self.secretary = self.people["secretary"]
        self.members = self.people["members"]
        self.outsider = make_user()
        self.session = VotingSession.objects.create(
            commission=self.commission, question="Утвердить план работы",
            deadline=timezone.now() + timedelta(days=3), initiated_by=self.secretary,
        )

    def everyone(self):
        return [self.chairman, self.secretary, *self.members]

    def open(self):
        services.open_session(self.session, self.secretary)
        self.session.refresh_from_db()
        return self.session

    def vote(self, user, choice="for", comment=""):
        self.client.force_login(user)
        return self.client.post(reverse("voting:vote", args=[self.session.pk]), {"choice": choice, "comment": comment})


class OpenSessionTests(VotingBase):
    def test_open_snapshots_eligible_and_sends_mail_with_link(self):
        mail.outbox.clear()
        self.client.force_login(self.secretary)
        response = self.client.post(reverse("voting:open", args=[self.session.pk]))
        self.assertRedirects(response, reverse("voting:detail", args=[self.session.pk]), fetch_redirect_response=False)
        self.session.refresh_from_db()
        self.assertEqual(self.session.status, VotingSession.Status.OPEN)
        self.assertEqual(self.session.eligible.count(), 7)
        self.assertEqual(self.session.quorum_needed, 5)
        self.assertEqual(len(mail.outbox), 7)
        message = mail.outbox[0]
        self.assertEqual(message.subject, "Голосование: Утвердить план работы")
        self.assertIn(f"/votings/{self.session.pk}/", message.body)
        self.assertEqual(Notification.objects.filter(event=Event.VOTING).count(), 7)

    def test_member_cannot_open(self):
        self.client.force_login(self.members[0])
        response = self.client.post(reverse("voting:open", args=[self.session.pk]))
        self.assertEqual(response.status_code, 403)

    def test_cannot_open_with_past_deadline(self):
        VotingSession.objects.filter(pk=self.session.pk).update(deadline=timezone.now() - timedelta(minutes=1))
        self.session.refresh_from_db()
        with self.assertRaises(ValidationError):
            services.open_session(self.session, self.secretary)

    def test_detail_requires_login_and_redirects_back(self):
        url = reverse("voting:detail", args=[self.session.pk])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 302)
        self.assertIn(f"next={url}", response["Location"])

    def test_default_deadline_is_workday_evening(self):
        deadline = timezone.localtime(services.default_deadline(self.commission))
        self.assertEqual((deadline.hour, deadline.minute), (18, 0))
        self.assertLess(deadline.weekday(), 5)


class CastVoteTests(VotingBase):
    def test_all_vote_auto_close_adopted(self):
        self.open()
        choices = ["for", "for", "for", "for", "against", "abstain", "for"]
        for user, choice in zip(self.everyone(), choices):
            response = self.vote(user, choice)
            self.assertEqual(response.status_code, 302)
        self.session.refresh_from_db()
        self.assertEqual(self.session.status, VotingSession.Status.CLOSED)
        self.assertEqual(self.session.outcome, O.ADOPTED)
        self.assertEqual((self.session.votes_for, self.session.votes_against, self.session.votes_abstain), (5, 1, 1))
        self.assertTrue(
            Notification.objects.filter(title__startswith="Итоги голосования", user=self.chairman).exists()
        )

    def test_not_closed_until_all_voted(self):
        self.open()
        for user in self.everyone()[:6]:
            self.vote(user)
        self.session.refresh_from_db()
        self.assertEqual(self.session.status, VotingSession.Status.OPEN)

    def test_rejected_when_for_not_majority(self):
        self.open()
        choices = ["for", "for", "for", "against", "against", "against", "abstain"]
        for user, choice in zip(self.everyone(), choices):
            self.vote(user, choice)
        self.session.refresh_from_db()
        self.assertEqual(self.session.outcome, O.REJECTED)

    def test_change_vote_allowed(self):
        self.open()
        self.vote(self.members[0], "for")
        self.vote(self.members[0], "against", "передумал")
        vote = Vote.objects.get(session=self.session, user=self.members[0])
        self.assertEqual(vote.choice, "against")
        self.assertEqual(vote.comment, "передумал")

    def test_change_vote_forbidden(self):
        self.commission.allow_vote_change = False
        self.commission.save()
        self.open()
        self.vote(self.members[0], "for")
        response = self.vote(self.members[0], "against")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(Vote.objects.get(session=self.session, user=self.members[0]).choice, "for")
        with self.assertRaises(ValidationError):
            services.cast_vote(self.session, self.members[0], "against")

    def test_non_member_cannot_vote(self):
        self.open()
        response = self.vote(self.outsider)
        self.assertEqual(response.status_code, 403)
        self.assertFalse(Vote.objects.filter(user=self.outsider).exists())

    def test_member_added_after_opening_cannot_vote(self):
        self.open()
        from apps.committees.models import MemberRecord

        newcomer = make_user()
        MemberRecord.objects.create(
            commission=self.commission, user=newcomer, role=MemberRecord.Role.MEMBER,
            start_date=timezone.localdate(),
        )
        response = self.vote(newcomer)
        self.assertEqual(response.status_code, 403)

    def test_cannot_vote_after_deadline(self):
        self.open()
        VotingSession.objects.filter(pk=self.session.pk).update(deadline=timezone.now() - timedelta(seconds=1))
        self.vote(self.members[0])
        self.assertFalse(Vote.objects.filter(session=self.session).exists())

    def test_comment_dropped_when_not_allowed(self):
        self.session.allow_comments = False
        self.session.save()
        self.open()
        self.vote(self.members[0], "for", "текст")
        self.assertEqual(Vote.objects.get(session=self.session, user=self.members[0]).comment, "")

    def test_detail_page_renders_for_member(self):
        self.open()
        self.client.force_login(self.members[0])
        response = self.client.get(reverse("voting:detail", args=[self.session.pk]))
        self.assertContains(response, "Проголосовать")
        self.assertContains(response, "не менее 2/3")

    def test_outsider_cannot_view(self):
        self.client.force_login(self.outsider)
        response = self.client.get(reverse("voting:detail", args=[self.session.pk]))
        self.assertEqual(response.status_code, 403)


class SecretaryEntryTests(VotingBase):
    def test_secretary_enters_vote_on_behalf(self):
        self.open()
        self.client.force_login(self.secretary)
        response = self.client.post(
            reverse("voting:enter_vote", args=[self.session.pk]),
            {"enter-user": self.members[1].pk, "enter-choice": "against", "enter-comment": "сказал на заседании"},
        )
        self.assertEqual(response.status_code, 302)
        vote = Vote.objects.get(session=self.session, user=self.members[1])
        self.assertEqual(vote.entered_by, self.secretary)
        self.assertEqual(vote.choice, "against")
        # отметка видна на странице
        response = self.client.get(reverse("voting:detail", args=[self.session.pk]))
        self.assertContains(response, "внесено секретарём")

    def test_member_cannot_enter_on_behalf(self):
        self.open()
        self.client.force_login(self.members[0])
        response = self.client.post(
            reverse("voting:enter_vote", args=[self.session.pk]),
            {"enter-user": self.members[1].pk, "enter-choice": "for"},
        )
        self.assertEqual(response.status_code, 403)
        self.assertFalse(Vote.objects.exists())

    def test_service_rejects_non_secretary(self):
        self.open()
        from django.core.exceptions import PermissionDenied

        with self.assertRaises(PermissionDenied):
            services.cast_vote(self.session, self.members[1], "for", entered_by=self.members[0])


class JobsTests(VotingBase):
    def test_expired_session_closed_by_job(self):
        self.open()
        self.vote(self.members[0])
        VotingSession.objects.filter(pk=self.session.pk).update(deadline=timezone.now() - timedelta(minutes=5))
        result = jobs.run()
        self.assertEqual(result["closed"], 1)
        self.session.refresh_from_db()
        self.assertEqual(self.session.status, VotingSession.Status.CLOSED)
        self.assertEqual(self.session.outcome, O.NO_QUORUM)
        self.assertIsNotNone(self.session.closed_at)

    def test_reminder_sent_once_to_non_voters(self):
        self.open()
        self.vote(self.members[0])
        VotingSession.objects.filter(pk=self.session.pk).update(deadline=timezone.now() + timedelta(hours=10))
        before = Notification.objects.filter(title__startswith="Напоминание").count()
        result = jobs.run()
        self.assertEqual(result["reminded"], 6)
        reminders = Notification.objects.filter(title__startswith="Напоминание")
        self.assertEqual(reminders.count() - before, 6)
        self.assertFalse(reminders.filter(user=self.members[0]).exists())
        jobs.run()
        self.assertEqual(Notification.objects.filter(title__startswith="Напоминание").count() - before, 6)
        self.session.refresh_from_db()
        self.assertTrue(self.session.reminder_sent)

    def test_no_reminder_far_from_deadline(self):
        self.open()
        self.assertEqual(jobs.run()["reminded"], 0)


class VisibilityTests(VotingBase):
    def close_with_votes(self):
        self.open()
        for user in self.everyone():
            self.vote(user, "for")
        self.session.refresh_from_db()

    def test_member_sees_nothing_while_open(self):
        self.open()
        vis = services.results_visible_to(self.session, self.members[0])
        self.assertEqual(vis, {"totals": False, "per_person": False, "progress_names": False})

    def test_manager_sees_everything_always(self):
        self.open()
        vis = services.results_visible_to(self.session, self.secretary)
        self.assertTrue(vis["totals"] and vis["per_person"] and vis["progress_names"])

    def test_open_voting_shows_names_to_members(self):
        self.close_with_votes()
        vis = services.results_visible_to(self.session, self.members[0])
        self.assertTrue(vis["totals"])
        self.assertTrue(vis["per_person"])

    def test_closed_voting_hides_names(self):
        self.commission.open_voting = False
        self.commission.save()
        self.close_with_votes()
        vis = services.results_visible_to(self.session, self.members[0])
        self.assertTrue(vis["totals"])
        self.assertFalse(vis["per_person"])
        self.assertFalse(services.results_visible_to(self.session, self.outsider)["totals"])

    def test_manager_sees_who_has_not_voted(self):
        self.open()
        self.vote(self.members[0])
        self.client.force_login(self.secretary)
        response = self.client.get(reverse("voting:detail", args=[self.session.pk]))
        self.assertContains(response, "Ещё не проголосовали")
        self.assertContains(response, self.members[1].short_name)


class CreateViewsTests(VotingBase):
    def test_create_for_item_prefilled(self):
        meeting = make_meeting(self.commission)
        item = AgendaItem.objects.create(meeting=meeting, title="Об утверждении СОП", description="Проект СОП")
        self.client.force_login(self.secretary)
        url = reverse("voting:create_for_item", args=[item.pk])
        response = self.client.get(url)
        self.assertContains(response, "Об утверждении СОП")
        deadline = (timezone.localtime() + timedelta(days=5)).strftime("%Y-%m-%dT%H:%M")
        response = self.client.post(url, {
            "question": "Об утверждении СОП", "description": "Проект СОП", "deadline": deadline, "allow_comments": "on",
        })
        session = VotingSession.objects.get(agenda_item=item)
        self.assertRedirects(response, reverse("voting:detail", args=[session.pk]), fetch_redirect_response=False)
        self.assertEqual(session.meeting, meeting)
        self.assertEqual(session.commission, self.commission)
        self.assertFalse(session.is_absentee)

    def test_create_for_item_forbidden_for_member(self):
        meeting = make_meeting(self.commission)
        item = AgendaItem.objects.create(meeting=meeting, title="Вопрос")
        self.client.force_login(self.members[0])
        response = self.client.get(reverse("voting:create_for_item", args=[item.pk]))
        self.assertEqual(response.status_code, 403)

    def test_create_absentee(self):
        self.client.force_login(self.secretary)
        url = reverse("voting:create") + f"?commission={self.commission.pk}"
        self.assertEqual(self.client.get(url).status_code, 200)
        deadline = (timezone.localtime() + timedelta(days=5)).strftime("%Y-%m-%dT%H:%M")
        response = self.client.post(reverse("voting:create"), {
            "commission": self.commission.pk, "question": "Заочный вопрос", "deadline": deadline,
        })
        self.assertEqual(response.status_code, 302)
        session = VotingSession.objects.get(question="Заочный вопрос")
        self.assertTrue(session.is_absentee)

    def test_edit_only_draft(self):
        self.open()
        self.client.force_login(self.secretary)
        response = self.client.get(reverse("voting:edit", args=[self.session.pk]))
        self.assertEqual(response.status_code, 302)

    def test_add_link_material_and_download(self):
        self.client.force_login(self.secretary)
        response = self.client.post(
            reverse("voting:material_add", args=[self.session.pk]),
            {"title": "Проект", "url": "https://example.kz/doc"},
        )
        self.assertEqual(response.status_code, 302)
        material = self.session.materials.get()
        self.client.force_login(self.outsider)
        self.assertEqual(self.client.get(reverse("voting:material_download", args=[material.pk])).status_code, 403)
        self.client.force_login(self.members[0])
        response = self.client.get(reverse("voting:material_download", args=[material.pk]))
        self.assertEqual(response["Location"], "https://example.kz/doc")

    def test_close_and_cancel(self):
        self.open()
        self.client.force_login(self.secretary)
        self.client.post(reverse("voting:close", args=[self.session.pk]))
        self.session.refresh_from_db()
        self.assertEqual(self.session.status, VotingSession.Status.CLOSED)
        other = VotingSession.objects.create(
            commission=self.commission, question="Другой", deadline=timezone.now() + timedelta(days=1)
        )
        self.client.post(reverse("voting:cancel", args=[other.pk]))
        other.refresh_from_db()
        self.assertEqual(other.status, VotingSession.Status.CANCELLED)

    def test_list_page(self):
        self.open()
        self.client.force_login(self.members[0])
        response = self.client.get(reverse("voting:list"))
        self.assertContains(response, "ожидает вашего голоса")
        response = self.client.get(reverse("voting:list"), {"status": "open", "commission": self.commission.pk})
        self.assertContains(response, "Утвердить план работы")

    def test_absentee_protocol_calls_service(self):
        self.open()
        services.close_session(self.session)
        fake = types.ModuleType("apps.protocols.services")
        protocol = mock.Mock(pk=42)
        fake.create_absentee_protocol = mock.Mock(return_value=protocol)
        self.client.force_login(self.secretary)
        url = reverse("voting:absentee_protocol")
        response = self.client.get(url, {"commission": self.commission.pk})
        self.assertContains(response, "Утвердить план работы")
        with mock.patch.dict(sys.modules, {"apps.protocols.services": fake}):
            response = self.client.post(url, {"commission": self.commission.pk, "sessions": [self.session.pk]})
        self.assertEqual(response.status_code, 302)
        args = fake.create_absentee_protocol.call_args[0]
        self.assertEqual([s.pk for s in args[0]], [self.session.pk])
        self.assertEqual(args[1], self.secretary)

    def test_absentee_protocol_forbidden_for_member(self):
        self.client.force_login(self.members[0])
        self.assertEqual(self.client.get(reverse("voting:absentee_protocol")).status_code, 403)
