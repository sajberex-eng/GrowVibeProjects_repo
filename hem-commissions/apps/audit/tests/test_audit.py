from django.test import TestCase
from django.urls import reverse

from apps.audit import services
from apps.audit.models import AuditLog
from apps.committees.testing import make_commission, make_user


class AuditViewsTests(TestCase):
    def setUp(self):
        self.admin = make_user(admin=True)
        self.user = make_user()
        self.commission, _ = make_commission(members=1)
        self.entry = services.log("sign", self.commission, {"hash": "abc", "список": [1, 2]}, user=self.admin, ip="10.0.0.1")

    def test_list_admin_only(self):
        self.client.force_login(self.user)
        self.assertEqual(self.client.get(reverse("audit:list")).status_code, 403)
        self.assertEqual(self.client.get(reverse("audit:detail", args=[self.entry.pk])).status_code, 403)

    def test_list_requires_login(self):
        self.assertEqual(self.client.get(reverse("audit:list")).status_code, 302)

    def test_list_and_filters(self):
        self.client.force_login(self.admin)
        response = self.client.get(reverse("audit:list"))
        self.assertEqual(response.status_code, 200)
        self.assertGreater(response.context["page"].paginator.count, 1)
        response = self.client.get(reverse("audit:list"), {"action": "sign"})
        self.assertEqual([e.pk for e in response.context["page"]], [self.entry.pk])
        response = self.client.get(reverse("audit:list"), {
            "user": self.admin.pk, "object_type": "committees.commission", "q": self.commission.short_name,
            "date_from": "2000-01-01", "date_to": "2100-01-01",
        })
        self.assertIn(self.entry.pk, [e.pk for e in response.context["page"]])
        response = self.client.get(reverse("audit:list"), {"date_to": "2000-01-01", "date_from": "bad"})
        self.assertEqual(response.context["page"].paginator.count, 0)

    def test_detail_pretty_json(self):
        self.client.force_login(self.admin)
        response = self.client.get(reverse("audit:detail", args=[self.entry.pk]))
        self.assertContains(response, "&quot;список&quot;")
        self.assertContains(response, "10.0.0.1")

    def test_no_edit_urls(self):
        self.client.force_login(self.admin)
        response = self.client.post(reverse("audit:detail", args=[self.entry.pk]), {"action": "x"})
        self.entry.refresh_from_db()
        self.assertEqual(self.entry.action, "sign")
        self.assertIn(response.status_code, (200, 405))


class AuditImmutabilityTests(TestCase):
    def setUp(self):
        self.entry = services.log("test", details={"a": 1})

    def test_save_existing_forbidden(self):
        self.entry.action = "changed"
        with self.assertRaises(PermissionError):
            self.entry.save()

    def test_delete_forbidden(self):
        with self.assertRaises(PermissionError):
            self.entry.delete()
        with self.assertRaises(PermissionError):
            AuditLog.objects.all().delete()

    def test_bulk_update_forbidden(self):
        with self.assertRaises(PermissionError):
            AuditLog.objects.filter(pk=self.entry.pk).update(action="x")
        self.assertEqual(AuditLog.objects.get(pk=self.entry.pk).action, "test")
