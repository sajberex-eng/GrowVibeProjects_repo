from datetime import timedelta

from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse
from django.utils import timezone

from apps.committees.models import (
    Commission, CommissionAccess, CompositionChange, LegalAct, MemberRecord, Order, Regulation, Rubric,
)
from apps.committees.testing import make_meeting, make_user
from apps.notifications.models import Notification

from .base import CommissionTestCase

PDF = b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF"


def pdf(name="scan.pdf"):
    return SimpleUploadedFile(name, PDF, content_type="application/pdf")


class ViewsRenderTests(CommissionTestCase):
    def setUp(self):
        super().setUp()
        LegalAct.objects.create(commission=self.commission, title="Приказ ДСМ", url="https://adilet.zan.kz/rus/docs/X")
        Rubric.objects.create(commission=self.commission, name="Рубрика")
        make_meeting(self.commission)

    def detail(self, tab, **params):
        url = reverse("committees:detail", args=[self.commission.pk]) + f"?tab={tab}"
        for k, v in params.items():
            url += f"&{k}={v}"
        return url

    def test_pages_for_admin(self):
        self.client.force_login(self.admin)
        today = timezone.localdate()
        urls = [
            reverse("committees:list"),
            reverse("committees:list") + "?view=table&all=1",
            reverse("committees:create"),
            reverse("committees:edit", args=[self.commission.pk]),
            reverse("committees:legal_monitor"),
            reverse("committees:legal_monitor") + "?status=attention",
            reverse("committees:legal_act_add", args=[self.commission.pk]),
            reverse("committees:rubric_add", args=[self.commission.pk]),
            self.detail("overview"),
            self.detail("composition"),
            self.detail("composition", on=(today - timedelta(days=400)).isoformat()),
            self.detail("composition", a=(today - timedelta(days=400)).isoformat(), b=today.isoformat()),
            self.detail("orders"),
            self.detail("regulations"),
            self.detail("legal"),
            self.detail("settings"),
        ]
        act = LegalAct.objects.get()
        urls += [
            reverse("committees:legal_act_edit", args=[self.commission.pk, act.pk]),
            reverse("committees:legal_act_replace", args=[self.commission.pk, act.pk]),
            reverse("committees:legal_act_log", args=[self.commission.pk, act.pk]),
        ]
        for url in urls:
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 200)

    def test_member_view_only(self):
        self.client.force_login(self.member)
        for tab in ("overview", "composition", "orders", "regulations", "legal"):
            with self.subTest(tab=tab):
                resp = self.client.get(self.detail(tab))
                self.assertEqual(resp.status_code, 200)
                self.assertNotContains(resp, "Проверить все сейчас")
        self.assertEqual(self.client.get(self.detail("settings")).status_code, 403)
        resp = self.client.post(reverse("committees:change_add", args=[self.commission.pk]), {
            "action": "remove", "user": self.members[1].pk,
        })
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(self.client.get(reverse("committees:legal_monitor")).status_code, 403)
        self.assertEqual(self.client.get(reverse("committees:create")).status_code, 403)
        self.assertEqual(self.client.post(reverse("committees:settings_edit", args=[self.commission.pk])).status_code, 403)

    def test_outsider_forbidden(self):
        self.client.force_login(self.outsider)
        self.assertEqual(self.client.get(self.detail("overview")).status_code, 403)
        resp = self.client.get(reverse("committees:list"))
        self.assertEqual(resp.status_code, 200)
        self.assertNotContains(resp, self.commission.name)

    def test_anonymous_redirect(self):
        resp = self.client.get(reverse("committees:list"))
        self.assertEqual(resp.status_code, 302)

    def test_secretary_can_manage_but_not_observers(self):
        self.client.force_login(self.secretary)
        self.assertEqual(self.client.get(self.detail("settings")).status_code, 200)
        resp = self.client.post(reverse("committees:observer_grant", args=[self.commission.pk]), {"user": self.outsider.pk})
        self.assertEqual(resp.status_code, 403)


class ManagementFlowTests(CommissionTestCase):
    def test_commission_create(self):
        self.client.force_login(self.admin)
        resp = self.client.post(reverse("committees:create"), {
            "name": "Комиссия по тестированию", "short_name": "КТ", "kind": "recommended",
            "is_active": "on", "meeting_quorum_numerator": 1, "meeting_quorum_denominator": 2,
            "meeting_quorum_strict": "on", "vote_quorum_numerator": 2, "vote_quorum_denominator": 3,
            "approval_days": 3, "vote_days": 3,
        })
        c = Commission.objects.get(short_name="КТ")
        self.assertRedirects(resp, reverse("committees:detail", args=[c.pk]))

    def test_settings_validation(self):
        self.client.force_login(self.chairman)
        url = reverse("committees:settings_edit", args=[self.commission.pk])
        data = {
            "meeting_quorum_numerator": 3, "meeting_quorum_denominator": 2,
            "vote_quorum_numerator": 2, "vote_quorum_denominator": 3, "approval_days": 3, "vote_days": 3,
        }
        self.assertEqual(self.client.post(url, data).status_code, 400)
        data["meeting_quorum_numerator"] = 2
        data["meeting_quorum_denominator"] = 3
        self.assertEqual(self.client.post(url, data).status_code, 302)
        self.commission.refresh_from_db()
        self.assertEqual(self.commission.meeting_quorum_denominator, 3)

    def test_order_flow(self):
        self.client.force_login(self.secretary)
        newbie = make_user()
        pk = self.commission.pk
        resp = self.client.post(reverse("committees:change_add", args=[pk]), {
            "action": "add", "user": newbie.pk, "new_role": "member", "basis": "заявление",
        })
        self.assertEqual(resp.status_code, 302)
        # добавить уже действующего члена нельзя
        resp = self.client.post(reverse("committees:change_add", args=[pk]), {"action": "add", "user": self.member.pk})
        self.assertEqual(resp.status_code, 400)
        change = CompositionChange.objects.get(user=newbie)

        resp = self.client.post(reverse("committees:order_create", args=[pk]), {
            "changes": [change.pk], "title": "О составе комиссии", "preamble": "",
        })
        self.assertEqual(resp.status_code, 302)
        order = Order.objects.get(commission=self.commission, status=Order.Status.DRAFT)
        change.refresh_from_db()
        self.assertEqual(change.order, order)

        resp = self.client.get(reverse("committees:order_draft", args=[pk, order.pk]))
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(b"".join(resp.streaming_content).startswith(b"PK"))

        # рядовой член не скачивает проект
        self.client.force_login(self.member)
        self.assertEqual(self.client.get(reverse("committees:order_draft", args=[pk, order.pk])).status_code, 403)
        self.client.force_login(self.secretary)

        self.assertEqual(self.client.get(reverse("committees:order_register", args=[pk, order.pk])).status_code, 200)
        signed_on = timezone.localdate() - timedelta(days=2)
        resp = self.client.post(reverse("committees:order_register", args=[pk, order.pk]), {
            "number": "77-од", "signed_on": signed_on.isoformat(), "scan_file": pdf(),
        })
        self.assertEqual(resp.status_code, 302)
        order.refresh_from_db()
        self.assertEqual(order.status, Order.Status.SIGNED)
        self.assertTrue(MemberRecord.objects.filter(user=newbie, start_date=signed_on, start_order=order).exists())
        self.assertTrue(Notification.objects.filter(user=newbie, event="composition").exists())
        self.assertTrue(Notification.objects.filter(user=self.chairman, event="composition").exists())

        self.client.force_login(self.member)
        resp = self.client.get(reverse("committees:order_scan", args=[pk, order.pk]))
        self.assertEqual(resp.status_code, 200)
        self.client.force_login(self.outsider)
        self.assertEqual(self.client.get(reverse("committees:order_scan", args=[pk, order.pk])).status_code, 403)

    def test_register_rejects_non_pdf(self):
        self.client.force_login(self.secretary)
        order = Order.objects.create(commission=self.commission, status=Order.Status.DRAFT)
        resp = self.client.post(reverse("committees:order_register", args=[self.commission.pk, order.pk]), {
            "number": "1", "signed_on": timezone.localdate().isoformat(),
            "scan_file": SimpleUploadedFile("scan.exe", b"MZ"),
        })
        self.assertEqual(resp.status_code, 200)
        order.refresh_from_db()
        self.assertEqual(order.status, Order.Status.DRAFT)

    def test_change_cancel_and_order_cancel(self):
        self.client.force_login(self.chairman)
        pk = self.commission.pk
        change = CompositionChange.objects.create(
            commission=self.commission, action="remove", user=self.members[2],
        )
        order = Order.objects.create(commission=self.commission, status=Order.Status.DRAFT)
        change.order = order
        change.save()
        self.client.post(reverse("committees:order_cancel", args=[pk, order.pk]))
        order.refresh_from_db()
        change.refresh_from_db()
        self.assertEqual(order.status, Order.Status.CANCELLED)
        self.assertIsNone(change.order)
        self.client.post(reverse("committees:change_cancel", args=[pk, change.pk]))
        change.refresh_from_db()
        self.assertEqual(change.status, CompositionChange.Status.CANCELLED)

    def test_regulation_versioning(self):
        self.client.force_login(self.secretary)
        pk = self.commission.pk
        today = timezone.localdate()
        url = reverse("committees:regulation_upload", args=[pk])
        resp = self.client.post(url, {
            "title": "Положение", "approved_on": (today - timedelta(days=300)).isoformat(),
            "order_number": "10", "file": pdf("reg1.pdf"),
        })
        self.assertEqual(resp.status_code, 302)
        resp = self.client.post(url, {
            "title": "Положение", "approved_on": (today - timedelta(days=5)).isoformat(),
            "review_on": (today + timedelta(days=360)).isoformat(), "file": pdf("reg2.pdf"),
        })
        self.assertEqual(resp.status_code, 302)
        v1, v2 = Regulation.objects.filter(commission=self.commission).order_by("version")
        self.assertEqual((v1.version, v2.version), (1, 2))
        self.assertEqual(v1.expired_on, v2.approved_on)
        self.assertIsNone(v2.expired_on)
        # более ранняя дата, чем у действующей, — ошибка
        resp = self.client.post(url, {
            "title": "Положение", "approved_on": (today - timedelta(days=100)).isoformat(), "file": pdf(),
        })
        self.assertEqual(resp.status_code, 400)
        # неверный тип файла
        resp = self.client.post(url, {
            "title": "Положение", "approved_on": today.isoformat(),
            "file": SimpleUploadedFile("x.exe", b"MZ"),
        })
        self.assertEqual(resp.status_code, 400)

        resp = self.client.get(reverse("committees:detail", args=[pk]) + "?tab=regulations")
        self.assertContains(resp, "утратила силу с")
        self.client.force_login(self.member)
        self.assertEqual(self.client.get(reverse("committees:regulation_download", args=[pk, v1.pk])).status_code, 200)
        self.client.force_login(self.outsider)
        self.assertEqual(self.client.get(reverse("committees:regulation_download", args=[pk, v1.pk])).status_code, 403)

    def test_legal_act_crud(self):
        self.client.force_login(self.admin)
        pk = self.commission.pk
        resp = self.client.post(reverse("committees:legal_act_add", args=[pk]), {
            "title": "Приказ А", "number": "1", "authority": "МЗ РК", "url": "https://adilet.zan.kz/rus/docs/A", "sort": 0,
        })
        self.assertEqual(resp.status_code, 302)
        act = LegalAct.objects.get(title="Приказ А")
        self.assertEqual(act.status, LegalAct.Status.NEEDS_CHECK)

        resp = self.client.post(reverse("committees:legal_act_check", args=[pk, act.pk]), {"next": "https://evil.example/"})
        self.assertEqual(resp.status_code, 302)
        self.assertNotIn("evil", resp["Location"])
        self.assertEqual(act.checks.count(), 1)

        self.client.post(reverse("committees:legal_check_commission", args=[pk]))
        self.assertEqual(act.checks.count(), 2)
        self.client.post(reverse("committees:legal_check_all"))
        self.assertEqual(act.checks.count(), 3)

        resp = self.client.post(reverse("committees:legal_act_replace", args=[pk, act.pk]), {
            "title": "Приказ Б", "number": "2", "authority": "МЗ РК", "url": "", "sort": 0,
        })
        self.assertEqual(resp.status_code, 302)
        act.refresh_from_db()
        new = LegalAct.objects.get(title="Приказ Б")
        self.assertEqual(act.replaced_by, new)
        self.assertEqual(act.status, LegalAct.Status.LOST_FORCE)

        self.client.post(reverse("committees:legal_act_retire", args=[pk, new.pk]))
        new.refresh_from_db()
        self.assertEqual(new.status, LegalAct.Status.RETIRED)

    def test_rubrics_and_observers(self):
        self.client.force_login(self.admin)
        pk = self.commission.pk
        self.client.post(reverse("committees:rubric_add", args=[pk]), {"name": "Новая", "sort": 1})
        rubric = Rubric.objects.get(commission=self.commission, name="Новая")
        resp = self.client.post(reverse("committees:rubric_add", args=[pk]), {"name": "новая", "sort": 1})
        self.assertEqual(resp.status_code, 400)
        self.client.post(reverse("committees:rubric_edit", args=[pk, rubric.pk]), {"name": "Переименована", "sort": 2})
        rubric.refresh_from_db()
        self.assertEqual(rubric.name, "Переименована")
        self.client.post(reverse("committees:rubric_delete", args=[pk, rubric.pk]))
        self.assertFalse(Rubric.objects.filter(pk=rubric.pk).exists())

        boss = make_user()
        self.client.post(reverse("committees:observer_grant", args=[pk]), {"user": boss.pk})
        grant = CommissionAccess.objects.get(commission=self.commission, user=boss)
        self.assertEqual(grant.granted_by, self.admin)
        self.client.force_login(boss)
        self.assertEqual(self.client.get(reverse("committees:detail", args=[pk])).status_code, 200)
        self.client.force_login(self.admin)
        self.client.post(reverse("committees:observer_revoke", args=[pk, grant.pk]))
        self.assertFalse(CommissionAccess.objects.filter(pk=grant.pk).exists())
