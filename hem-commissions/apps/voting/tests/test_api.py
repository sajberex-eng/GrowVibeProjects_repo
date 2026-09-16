from datetime import date, timedelta

from django.test import TestCase
from django.utils import timezone

from apps.committees.models import LegalAct
from apps.committees.testing import make_commission, make_meeting, make_user
from apps.meetings.models import AgendaItem
from apps.protocols.models import Assignment, AssignmentComment, Decision, Protocol, ProtocolItem
from apps.voting import services
from apps.voting.models import VotingSession


class ApiTests(TestCase):
    def setUp(self):
        self.c1, p1 = make_commission(members=2)
        self.c2, p2 = make_commission(members=2)
        self.member = p1["members"][0]
        self.secretary = p1["secretary"]
        self.other_member = p2["members"][0]
        self.m1 = make_meeting(self.c1)
        self.m2 = make_meeting(self.c2)
        AgendaItem.objects.create(meeting=self.m1, title="Вопрос 1")
        LegalAct.objects.create(commission=self.c1, title="Приказ МЗ РК")
        LegalAct.objects.create(commission=self.c2, title="Чужой НПА")
        self.v1 = VotingSession.objects.create(commission=self.c1, question="Вопрос К1", deadline=timezone.now() + timedelta(days=2))
        self.v2 = VotingSession.objects.create(commission=self.c2, question="Вопрос К2", deadline=timezone.now() + timedelta(days=2))
        self.p1 = Protocol.objects.create(commission=self.c1, meeting=self.m1, protocol_date=date.today(), status=Protocol.Status.SIGNED)
        self.p2 = Protocol.objects.create(commission=self.c2, meeting=self.m2, protocol_date=date.today(), status=Protocol.Status.SIGNED)
        item1 = ProtocolItem.objects.create(protocol=self.p1, title="Вопрос")
        item2 = ProtocolItem.objects.create(protocol=self.p2, title="Вопрос")
        d1 = Decision.objects.create(protocol=self.p1, item=item1, text="Решение 1")
        d2 = Decision.objects.create(protocol=self.p2, item=item2, text="Решение 2")
        self.a1 = Assignment.objects.create(decision=d1, text="Сделать", responsible=self.member, due_date=date.today() + timedelta(days=5))
        self.a2 = Assignment.objects.create(decision=d2, text="Чужое", responsible=self.other_member, due_date=date.today() - timedelta(days=5))
        self.client.force_login(self.member)

    def ids(self, url, **params):
        response = self.client.get(url, params)
        self.assertEqual(response.status_code, 200, response.content[:500])
        return {row["id"] for row in response.json()["results"]}

    def test_requires_auth(self):
        self.client.logout()
        self.assertIn(self.client.get("/api/commissions/").status_code, (401, 403))

    def test_lists_filtered_by_access(self):
        self.assertEqual(self.ids("/api/commissions/"), {self.c1.pk})
        self.assertEqual(self.ids("/api/meetings/"), {self.m1.pk})
        self.assertEqual(self.ids("/api/legal-acts/"), set(LegalAct.objects.filter(commission=self.c1).values_list("pk", flat=True)))
        self.assertEqual(self.ids("/api/votings/"), {self.v1.pk})
        self.assertEqual(self.ids("/api/protocols/"), {self.p1.pk})
        self.assertEqual(self.ids("/api/assignments/"), {self.a1.pk})
        self.assertEqual(self.ids("/api/orders/"), set(self.c1.orders.values_list("pk", flat=True)))
        history = self.ids("/api/members-history/")
        self.assertEqual(history, set(self.c1.member_records.values_list("pk", flat=True)))
        self.assertEqual(self.client.get(f"/api/commissions/{self.c2.pk}/").status_code, 404)
        self.assertEqual(self.client.get(f"/api/meetings/{self.m2.pk}/agenda/").status_code, 404)

    def test_nested_actions(self):
        response = self.client.get(f"/api/commissions/{self.c1.pk}/members/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json()), 4)
        response = self.client.get(f"/api/meetings/{self.m1.pk}/agenda/")
        self.assertEqual(response.json()[0]["title"], "Вопрос 1")
        response = self.client.get(f"/api/protocols/{self.p1.pk}/")
        data = response.json()
        self.assertEqual(data["decisions"][0]["text"], "Решение 1")
        self.assertIn("verify", data["links"])

    def test_assignment_filters(self):
        self.assertEqual(self.ids("/api/assignments/", responsible="me"), {self.a1.pk})
        self.assertEqual(self.ids("/api/assignments/", status="overdue"), set())
        self.assertEqual(self.ids("/api/assignments/", status="assigned", commission=self.c1.pk), {self.a1.pk})

    def test_assignment_report(self):
        response = self.client.post(f"/api/assignments/{self.a1.pk}/report/", {"comment": "Готово"}, content_type="application/json")
        self.assertEqual(response.status_code, 200, response.content)
        self.a1.refresh_from_db()
        self.assertEqual(self.a1.status, Assignment.Status.ON_CONFIRMATION)
        self.assertIsNotNone(self.a1.reported_at)
        self.assertTrue(AssignmentComment.objects.filter(assignment=self.a1, text="Готово").exists())
        # повторно — нельзя
        response = self.client.post(f"/api/assignments/{self.a1.pk}/report/", {}, content_type="application/json")
        self.assertEqual(response.status_code, 400)

    def test_assignment_report_only_responsible(self):
        self.client.force_login(self.secretary)
        response = self.client.post(f"/api/assignments/{self.a1.pk}/report/", {}, content_type="application/json")
        self.assertEqual(response.status_code, 403)

    def test_voting_votes_hidden_unless_allowed(self):
        self.c1.open_voting = False
        self.c1.save()
        services.open_session(self.v1, self.secretary)
        services.cast_vote(self.v1, self.member, "for")
        services.close_session(self.v1)
        data = self.client.get(f"/api/votings/{self.v1.pk}/").json()
        self.assertIsNone(data["votes"])
        self.assertEqual(data["results"]["votes_for"], 1)
        self.client.force_login(self.secretary)
        data = self.client.get(f"/api/votings/{self.v1.pk}/").json()
        self.assertEqual(len(data["votes"]), 1)

    def test_schema_generates(self):
        response = self.client.get("/api/schema/")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"/api/assignments/{id}/report/", response.content)

    def test_outsider_sees_nothing(self):
        self.client.force_login(make_user())
        self.assertEqual(self.ids("/api/commissions/"), set())
        self.assertEqual(self.ids("/api/assignments/"), set())
