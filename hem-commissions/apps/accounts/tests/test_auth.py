from datetime import timedelta

from django.core import mail
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import OneTimeCode, User
from apps.audit.models import AuditLog
from apps.committees.testing import make_user


class LoginLockoutTests(TestCase):
    def setUp(self):
        self.user = make_user("ivanov")

    def _login(self, password):
        return self.client.post(reverse("accounts:login"), {"username": "ivanov", "password": password})

    def test_successful_login_resets_counter(self):
        self._login("wrong")
        response = self._login("Passw0rd!Test")
        self.assertRedirects(response, reverse("dashboard"), fetch_redirect_response=False)
        self.user.refresh_from_db()
        self.assertEqual(self.user.failed_login_attempts, 0)

    @override_settings(LOGIN_MAX_FAILED_ATTEMPTS=5)
    def test_locked_after_five_failures(self):
        for _ in range(5):
            self._login("wrong")
        self.user.refresh_from_db()
        self.assertTrue(self.user.is_locked)
        response = self._login("Passw0rd!Test")
        self.assertContains(response, "заблокирована")
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_lock_expires(self):
        self.user.locked_until = timezone.now() - timedelta(minutes=1)
        self.user.save()
        response = self._login("Passw0rd!Test")
        self.assertEqual(response.status_code, 302)

    def test_failed_login_is_audited(self):
        self._login("wrong")
        self.assertTrue(AuditLog.objects.filter(action="login_failed").exists())


class ForcedPasswordChangeTests(TestCase):
    def test_redirects_until_password_changed(self):
        user = make_user("newbie", must_change_password=True)
        self.client.force_login(user)
        response = self.client.get(reverse("dashboard"))
        self.assertRedirects(response, reverse("accounts:password_change"))
        response = self.client.post(reverse("accounts:password_change"), {
            "old_password": "Passw0rd!Test",
            "new_password1": "NovyiParol2026",
            "new_password2": "NovyiParol2026",
        })
        self.assertEqual(response.status_code, 302)
        user.refresh_from_db()
        self.assertFalse(user.must_change_password)
        self.assertEqual(self.client.get(reverse("dashboard")).status_code, 200)

    def test_complexity_validator(self):
        user = make_user("weak", must_change_password=True)
        self.client.force_login(user)
        response = self.client.post(reverse("accounts:password_change"), {
            "old_password": "Passw0rd!Test",
            "new_password1": "alllowercase123",
            "new_password2": "alllowercase123",
        })
        self.assertContains(response, "прописные")


class SessionTimeoutTests(TestCase):
    @override_settings(SESSION_IDLE_TIMEOUT_MINUTES=1)
    def test_idle_session_logged_out(self):
        user = make_user()
        self.client.force_login(user)
        self.client.get(reverse("dashboard"))
        session = self.client.session
        session["last_activity"] -= 120
        session.save()
        response = self.client.get(reverse("dashboard"))
        self.assertEqual(response.status_code, 302)
        self.assertIn("timeout=1", response.url)


class UserAdminTests(TestCase):
    def setUp(self):
        self.admin = make_user("boss", admin=True)
        self.client.force_login(self.admin)

    def test_non_admin_forbidden(self):
        self.client.force_login(make_user())
        self.assertEqual(self.client.get(reverse("accounts:user_list")).status_code, 403)

    def test_create_user_sends_temporary_password(self):
        response = self.client.post(reverse("accounts:user_create"), {
            "username": "petrova", "last_name": "Петрова", "first_name": "Анна",
            "email": "petrova@example.kz", "is_active": "on",
        })
        self.assertEqual(response.status_code, 302)
        user = User.objects.get(username="petrova")
        self.assertTrue(user.must_change_password)
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("Временный пароль", mail.outbox[0].body)

    def test_dismissal_deactivates(self):
        user = make_user("leaver")
        self.client.post(reverse("accounts:user_edit", args=[user.pk]), {
            "username": "leaver", "last_name": "Уходящий", "first_name": "Сотрудник",
            "email": "leaver@example.kz", "is_active": "on", "dismissed_at": "2026-09-01",
        })
        user.refresh_from_db()
        self.assertFalse(user.is_active)
        self.assertEqual(str(user.dismissed_at), "2026-09-01")


class OneTimeCodeTests(TestCase):
    def test_issue_and_verify_once(self):
        user = make_user()
        code = OneTimeCode.issue(user, "sign:1")
        self.assertFalse(OneTimeCode.verify(user, "sign:1", "000000" if code != "000000" else "111111"))
        self.assertTrue(OneTimeCode.verify(user, "sign:1", code))
        self.assertFalse(OneTimeCode.verify(user, "sign:1", code))

    def test_attempts_limited(self):
        user = make_user()
        code = OneTimeCode.issue(user, "p")
        wrong = "000000" if code != "000000" else "111111"
        for _ in range(5):
            OneTimeCode.verify(user, "p", wrong)
        self.assertFalse(OneTimeCode.verify(user, "p", code))


class AuditImmutabilityTests(TestCase):
    def test_cannot_modify_or_delete(self):
        entry = AuditLog.objects.create(action="test")
        entry.action = "changed"
        with self.assertRaises(PermissionError):
            entry.save()
        with self.assertRaises(PermissionError):
            entry.delete()
        with self.assertRaises(PermissionError):
            AuditLog.objects.all().delete()
