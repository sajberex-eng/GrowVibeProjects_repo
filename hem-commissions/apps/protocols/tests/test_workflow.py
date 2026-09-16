import hashlib
from datetime import datetime, time, timedelta

from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse
from django.utils import timezone

from apps.committees.models import CommissionAccess
from apps.committees.testing import make_user
from apps.committees.workdays import add_workdays
from apps.notifications.models import Event, Notification

from .. import jobs, services
from ..models import Assignment, DissentingOpinion, Protocol, ProtocolApproval, Signature
from .base import ProtocolTestCase


class ApprovalTests(ProtocolTestCase):
    def test_send_to_approval(self):
        protocol = self.make_draft()
        services.send_to_approval(protocol, self.secretary)
        protocol.refresh_from_db()
        self.assertEqual(protocol.status, Protocol.Status.APPROVAL)
        self.assertEqual(protocol.approval_deadline, add_workdays(timezone.localdate(), 3))
        recipients = set(protocol.approvals.values_list("user_id", flat=True))
        self.assertEqual(recipients, {self.chairman.pk, *[m.pk for m in self.members]})
        note = Notification.objects.filter(event=Event.PROTOCOL_APPROVAL, user=self.members[0]).get()
        self.assertIn("Отсутствие ответа", note.body)

    def test_respond_and_early_move_blocked(self):
        protocol = self.make_draft()
        services.send_to_approval(protocol, self.secretary)
        self.login(self.members[0])
        url = reverse("protocols:respond_approval", args=[protocol.pk])
        self.client.post(url, {"status": "remarks", "remarks": ""})
        self.assertEqual(ProtocolApproval.objects.get(user=self.members[0]).status, "pending")
        item = protocol.items.first()
        self.client.post(url, {"status": "remarks", "remarks": "Уточнить формулировку", "item": item.pk})
        row = ProtocolApproval.objects.get(user=self.members[0])
        self.assertEqual((row.status, row.item_id), ("remarks", item.pk))

        # посторонний не может ответить
        with self.assertRaises(ValidationError):
            services.respond_approval(protocol, self.outsider, "agreed")

        with self.assertRaises(ValidationError):
            services.move_to_signing(protocol, self.secretary)
        self.login(self.secretary)
        self.client.post(reverse("protocols:move_to_signing", args=[protocol.pk]))
        self.assertEqual(self.reload(protocol).status, Protocol.Status.APPROVAL)

        for user in [self.chairman, *self.members[1:]]:
            services.respond_approval(protocol, user, "agreed")
        self.client.post(reverse("protocols:move_to_signing", args=[protocol.pk]))
        protocol.refresh_from_db()
        self.assertEqual(protocol.status, Protocol.Status.SIGNING)
        self.assertTrue(protocol.frozen_hash)

    def test_new_revision(self):
        protocol = self.make_draft()
        services.send_to_approval(protocol, self.secretary)
        services.respond_approval(protocol, self.members[0], "remarks", "Исправить")
        services.new_revision(protocol, self.secretary)
        self.assertEqual(protocol.status, Protocol.Status.DRAFT)
        services.send_to_approval(protocol, self.secretary)
        protocol.refresh_from_db()
        self.assertEqual(protocol.revision, 2)
        self.assertEqual(protocol.approvals.filter(revision=2).count(), 4)
        self.assertEqual(protocol.approvals.filter(revision=2, status="pending").count(), 4)
        self.assertEqual(protocol.approvals.filter(revision=1, status="remarks").count(), 1)

    def test_jobs_reminder_and_auto_move(self):
        protocol = self.make_draft()
        services.send_to_approval(protocol, self.secretary)
        services.respond_approval(protocol, self.members[0], "agreed")
        deadline = protocol.approval_deadline

        day_before = timezone.make_aware(datetime.combine(deadline - timedelta(days=1), time(9, 0)))
        stats = jobs.run(now=day_before)
        self.assertEqual(stats["approval_reminders"], 3)
        self.assertEqual(jobs.run(now=day_before)["approval_reminders"], 0)  # только один раз
        reminded = Notification.objects.filter(title__startswith="Напоминание: согласование").values_list("user_id", flat=True)
        self.assertNotIn(self.members[0].pk, reminded)

        on_deadline = day_before + timedelta(days=1)
        self.assertEqual(jobs.run(now=on_deadline)["moved_to_signing"], 0)
        after = day_before + timedelta(days=2)
        stats = jobs.run(now=after)
        self.assertEqual(stats["moved_to_signing"], 1)
        protocol.refresh_from_db()
        self.assertEqual(protocol.status, Protocol.Status.SIGNING)
        summary = Notification.objects.get(user=self.chairman, title__startswith="Итоги согласования")
        self.assertIn(self.members[1].full_name, summary.body)
        self.assertNotIn(self.members[0].full_name, summary.body)
        self.assertTrue(Notification.objects.filter(user=self.secretary, event=Event.PROTOCOL_SIGNING).exists())


class DissentTests(ProtocolTestCase):
    def test_dissent_only_during_approval_with_otp(self):
        protocol = self.make_draft()
        member = self.members[0]
        item = protocol.items.first()
        with self.assertRaises(ValidationError):
            services.request_dissent_code(protocol, member)  # черновик

        services.send_to_approval(protocol, self.secretary)
        self.login(member)
        self.client.post(reverse("protocols:dissent_code", args=[protocol.pk]))
        code = self.last_code(member)
        # неверный код
        response = self.client.post(reverse("protocols:dissent_add", args=[protocol.pk]), {
            "item": item.pk, "text": "Не согласен", "code": "000000" if code != "000000" else "111111",
        })
        self.assertEqual(DissentingOpinion.objects.count(), 0)
        response = self.client.post(reverse("protocols:dissent_add", args=[protocol.pk]), {
            "item": item.pk, "text": "Не согласен с решением", "code": code,
            "file": SimpleUploadedFile("opinion.pdf", b"%PDF-1.4 test", content_type="application/pdf"),
        })
        self.assertEqual(response.status_code, 302)
        dissent = DissentingOpinion.objects.get()
        self.assertEqual(dissent.author, member)
        self.assertIsNotNone(dissent.signed_at)
        self.assertEqual(len(dissent.text_hash), 64)
        self.assertEqual(dissent.file_name, "opinion.pdf")

        snap = services.build_snapshot(protocol)
        self.assertIn(f"С особым мнением члена комиссии {member.full_name}.", snap["items"][0]["dissent_note"])
        self.assertEqual(snap["dissents"][0]["author"], member.full_name)
        page = self.client.get(reverse("protocols:detail", args=[protocol.pk]))
        self.assertContains(page, "С особым мнением члена комиссии")

        # чужой не может отозвать
        with self.assertRaises(ValidationError):
            services.withdraw_dissent(dissent, self.members[1])
        self.client.post(reverse("protocols:dissent_withdraw", args=[dissent.pk]))
        self.assertIsNotNone(self.reload(dissent).withdrawn_at)
        self.assertEqual(services.build_snapshot(protocol)["dissents"], [])

    def test_dissent_rejected_after_deadline(self):
        protocol = self.make_draft()
        services.send_to_approval(protocol, self.secretary)
        member = self.members[0]
        services.request_dissent_code(protocol, member)
        code = self.last_code(member)
        protocol.approval_deadline = timezone.localdate() - timedelta(days=1)
        protocol.save()
        with self.assertRaises(ValidationError):
            services.add_dissent(protocol, member, protocol.items.first(), "Поздно", None, code)
        self.assertFalse(DissentingOpinion.objects.exists())

    def test_outsider_cannot_dissent(self):
        protocol = self.make_draft()
        services.send_to_approval(protocol, self.secretary)
        with self.assertRaises(ValidationError):
            services.request_dissent_code(protocol, self.admin)


class SigningTests(ProtocolTestCase):
    def test_freeze_hash_matches_pdf(self):
        protocol = self.make_signing()
        with protocol.frozen_pdf.open("rb") as fh:
            data = fh.read()
        self.assertTrue(data.startswith(b"%PDF"))
        self.assertEqual(hashlib.sha256(data).hexdigest(), protocol.frozen_hash)
        self.assertEqual(protocol.snapshot["number"], protocol.number)
        self.assertEqual(len(protocol.snapshot["attendance"]["present"]), 4)
        self.assertEqual(protocol.snapshot["items"][0]["decisions"][0]["assignments"][0]["text"], "Подготовить отчёт")
        # детерминированность: тот же снимок — тот же PDF
        from .. import pdf

        self.assertEqual(hashlib.sha256(pdf.render_protocol(protocol.snapshot)).hexdigest(), protocol.frozen_hash)

    def test_sign_order_and_codes(self):
        protocol = self.make_signing()
        # председатель не может подписать первым
        with self.assertRaises(ValidationError):
            services.request_sign_code(protocol, self.chairman)
        self.assertFalse(services.can_sign(protocol, self.chairman))
        # администратор не подписывает за секретаря
        with self.assertRaises(ValidationError):
            services.request_sign_code(protocol, self.admin)

        services.request_sign_code(protocol, self.secretary)
        code = self.last_code(self.secretary)
        wrong = "000000" if code != "000000" else "111111"
        with self.assertRaises(ValidationError):
            services.sign(protocol, self.secretary, wrong)
        self.assertEqual(Signature.objects.count(), 0)

        self.login(self.secretary)
        response = self.client.post(reverse("protocols:sign", args=[protocol.pk]), {"code": code})
        self.assertEqual(response.status_code, 302)
        sig = Signature.objects.get()
        self.assertEqual((sig.user, sig.role, sig.document_hash), (self.secretary, "secretary", protocol.frozen_hash))
        self.assertTrue(Notification.objects.filter(user=self.chairman, event=Event.PROTOCOL_SIGNING).exists())
        self.login(self.chairman)
        page = self.client.get(reverse("protocols:detail", args=[protocol.pk]))
        self.assertContains(page, "Получить код на e-mail")
        self.assertContains(page, "Вернуть на доработку")
        self.login(self.secretary)
        self.assertNotContains(self.client.get(reverse("protocols:detail", args=[protocol.pk])), "Получить код на e-mail")

        # код секретаря уже использован
        with self.assertRaises(ValidationError):
            services.sign(protocol, self.secretary, code)

        observer = make_user()
        CommissionAccess.objects.create(commission=self.commission, user=observer)
        self.sign_as(protocol, self.chairman)
        protocol.refresh_from_db()
        self.assertEqual(protocol.status, Protocol.Status.SIGNED)
        self.assertIsNotNone(protocol.signed_at)
        with protocol.signed_pdf.open("rb") as fh:
            signed = fh.read()
        self.assertEqual(hashlib.sha256(signed).hexdigest(), protocol.signed_hash)
        self.assertNotEqual(protocol.signed_hash, protocol.frozen_hash)
        self.assertTrue(Notification.objects.filter(user=observer, event=Event.PROTOCOL_SIGNED).exists())
        self.assertTrue(Notification.objects.filter(user=self.members[0], event=Event.PROTOCOL_SIGNED).exists())
        # поручение — ответственному и соисполнителю
        self.assertTrue(Notification.objects.filter(user=self.members[0], event=Event.ASSIGNMENT).exists())
        self.assertTrue(Notification.objects.filter(user=self.members[1], event=Event.ASSIGNMENT).exists())
        # в уведомлениях нет содержания протокола
        for note in Notification.objects.all():
            self.assertNotIn("Утвердить план", note.body)

        page = self.client.get(reverse("protocols:detail", args=[protocol.pk]))
        self.assertContains(page, protocol.signed_hash)
        self.assertEqual(self.client.get(reverse("protocols:download_signed", args=[protocol.pk])).status_code, 200)
        docx = self.client.get(reverse("protocols:export_docx", args=[protocol.pk]))
        self.assertTrue(docx.content.startswith(b"PK"))

    def test_verify_page_public_and_hash_compare(self):
        protocol = self.make_signed()
        self.client.logout()
        url = reverse("verify", args=[protocol.uid])
        page = self.client.get(url)
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, protocol.frozen_hash)
        self.assertContains(page, protocol.signed_hash)
        self.assertContains(page, self.secretary.full_name)
        self.assertContains(page, self.commission.name)
        self.assertNotContains(page, "Подготовить отчёт")
        self.assertNotContains(page, "Утвердить план")

        with protocol.signed_pdf.open("rb") as fh:
            signed = fh.read()
        response = self.client.post(url, {"file": SimpleUploadedFile("p.pdf", signed, content_type="application/pdf")})
        self.assertContains(response, "Совпадает")
        self.assertEqual(response.context["result"]["match"], "signed")
        with protocol.frozen_pdf.open("rb") as fh:
            frozen = fh.read()
        response = self.client.post(url, {"file": SimpleUploadedFile("p.pdf", frozen)})
        self.assertEqual(response.context["result"]["match"], "frozen")
        response = self.client.post(url, {"file": SimpleUploadedFile("p.pdf", signed + b"x")})
        self.assertIsNone(response.context["result"]["match"])
        self.assertContains(response, "Не совпадает")

        # аннулированный — крупное предупреждение
        services.annul(protocol, self.chairman, "Ошибка в составе")
        self.assertContains(self.client.get(url), "ПРОТОКОЛ АННУЛИРОВАН")

    def test_return_for_rework(self):
        protocol = self.make_signing()
        self.sign_as(protocol, self.secretary)
        frozen_name = protocol.frozen_pdf.name
        with self.assertRaises(ValidationError):
            services.return_for_rework(protocol, self.secretary, "Причина")
        self.login(self.chairman)
        self.client.post(reverse("protocols:return_for_rework", args=[protocol.pk]), {"reason": "Уточнить решение"})
        protocol.refresh_from_db()
        self.assertEqual(protocol.status, Protocol.Status.DRAFT)
        self.assertEqual(protocol.frozen_hash, "")
        self.assertFalse(protocol.frozen_pdf)
        self.assertFalse(protocol.frozen_pdf.storage.exists(frozen_name))
        sig = Signature.objects.get()
        self.assertIsNotNone(sig.revoked_at)
        self.assertEqual(sig.revoke_reason, "Уточнить решение")
        self.assertEqual(protocol.active_signatures().count(), 0)
        services.send_to_approval(protocol, self.secretary)
        protocol.refresh_from_db()
        self.assertEqual(protocol.revision, 2)
        for row in services.current_approvals(protocol):
            services.respond_approval(protocol, row.user, "agreed")
        services.move_to_signing(protocol, self.secretary)
        self.assertEqual(protocol.next_signer_role(), "secretary")

    def test_annul_and_replacement(self):
        protocol = self.make_signed()
        with self.assertRaises(ValidationError):
            services.annul(protocol, self.secretary, "Причина")
        with self.assertRaises(ValidationError):
            services.annul(protocol, self.chairman, "")
        self.login(self.admin)
        self.client.post(reverse("protocols:annul", args=[protocol.pk]), {"reason": "Техническая ошибка"})
        protocol.refresh_from_db()
        self.assertEqual(protocol.status, Protocol.Status.ANNULLED)
        self.assertEqual(protocol.annulled_reason, "Техническая ошибка")
        self.assertEqual(Assignment.objects.get().status, Assignment.Status.CANCELLED)

        self.login(self.secretary)
        response = self.client.post(reverse("protocols:create_replacement", args=[protocol.pk]))
        new = Protocol.objects.get(replaces=protocol)
        self.assertRedirects(response, reverse("protocols:detail", args=[new.pk]))
        self.assertEqual(new.status, Protocol.Status.DRAFT)
        self.assertNotEqual(new.number, protocol.number)
        self.assertEqual(new.items.count(), protocol.items.count())
        self.assertEqual(new.decisions.count(), 1)
        copied = Assignment.objects.get(decision__protocol=new)
        self.assertEqual(copied.status, Assignment.Status.ASSIGNED)
        self.assertEqual(list(copied.co_executors.all()), [self.members[1]])
        self.assertContains(self.client.get(reverse("protocols:detail", args=[protocol.pk])), "Протокол аннулирован")

    def test_annul_only_signed(self):
        protocol = self.make_draft()
        with self.assertRaises(ValidationError):
            services.annul(protocol, self.chairman, "x")
