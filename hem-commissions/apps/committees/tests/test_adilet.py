from unittest import mock

from django.test import SimpleTestCase, override_settings

from apps.committees import adilet, jobs
from apps.committees.models import LegalAct, LegalActCheck
from apps.notifications.models import Notification

from .base import CommissionTestCase

S = LegalAct.Status
URL = "https://adilet.zan.kz/rus/docs/V2000021727"

HEADER_ACTUAL = (
    "Об утверждении правил организации и проведения внутренней и внешней экспертиз качества медицинских услуг\n"
    "Приказ Министра здравоохранения Республики Казахстан от 3 декабря 2020 года № ҚР ДСМ-230/2020.\n"
    "Статус документа: Действующий\n"
)
HEADER_LOST = (
    "Об утверждении Правил ...\n"
    "Приказ Министра здравоохранения Республики Казахстан от 5 мая 2015 года № 32.\n"
    "Утратил силу приказом Министра здравоохранения Республики Казахстан от 20.10.2020 № ҚР ДСМ-140/2020.\n"
)
BODY_WITH_AMENDMENTS = (
    "Об утверждении правил\nПриказ Министра ...\n" + "Текст документа. " * 300
    + "Пункт 5 утратил силу приказом Министра здравоохранения РК от 01.01.2022."
)


class ClassifyPageTests(SimpleTestCase):
    def test_actual(self):
        self.assertEqual(adilet.classify_page(200, URL, HEADER_ACTUAL)[0], S.ACTUAL)

    def test_kazakh_new(self):
        self.assertEqual(adilet.classify_page(200, URL, "Бұйрық ... Жаңа\nмәтін")[0], S.ACTUAL)

    def test_lost_force(self):
        self.assertEqual(adilet.classify_page(200, URL, HEADER_LOST)[0], S.LOST_FORCE)
        self.assertEqual(adilet.classify_page(200, URL, "Приказ № 1\nСтатус: Утративший силу")[0], S.LOST_FORCE)
        self.assertEqual(adilet.classify_page(200, URL, "Постановление\nНе действует с 01.01.2021")[0], S.LOST_FORCE)
        self.assertEqual(adilet.classify_page(200, URL, "Закон\nСтатус: Утратила силу")[0], S.LOST_FORCE)

    def test_not_actual_word(self):
        # «недействующий» — не «действующий»
        self.assertEqual(adilet.classify_page(200, URL, "Приказ\nНедействующий документ")[0], S.LOST_FORCE)

    def test_unavailable(self):
        self.assertEqual(adilet.classify_page(404, URL, "")[0], S.UNAVAILABLE)
        self.assertEqual(adilet.classify_page(502, URL, "Bad gateway")[0], S.UNAVAILABLE)
        self.assertEqual(adilet.classify_page(None, URL, "")[0], S.UNAVAILABLE)
        self.assertEqual(adilet.classify_page(200, URL, "   ")[0], S.UNAVAILABLE)
        self.assertEqual(adilet.classify_page(200, URL, "Страница не найдена")[0], S.UNAVAILABLE)
        self.assertEqual(adilet.classify_page(200, "https://adilet.zan.kz/rus", "Главная")[0], S.UNAVAILABLE)

    def test_unknown_is_needs_check(self):
        self.assertEqual(adilet.classify_page(200, URL, "Какой-то текст без статуса")[0], S.NEEDS_CHECK)
        self.assertEqual(adilet.classify_page(200, URL, BODY_WITH_AMENDMENTS)[0], S.NEEDS_CHECK)

    def test_conflicting_header(self):
        text = "Приказ\nДействующий\nПункт 3 утратил силу"
        self.assertEqual(adilet.classify_page(200, URL, text)[0], S.NEEDS_CHECK)


class CheckActTests(CommissionTestCase):
    def setUp(self):
        super().setUp()
        self.act = LegalAct.objects.create(commission=self.commission, title="Приказ", url=URL, status=S.ACTUAL)

    def test_playwright_missing(self):
        with mock.patch.object(adilet, "playwright_available", return_value=False):
            check = adilet.check_act(self.act, manual=True, user=self.admin)
        self.act.refresh_from_db()
        self.assertEqual(check.result, S.NEEDS_CHECK)
        self.assertIn("Playwright не установлен", check.details)
        self.assertEqual(self.act.status, S.NEEDS_CHECK)
        self.assertIsNotNone(self.act.last_checked_at)
        self.assertTrue(check.manual)
        self.assertEqual(check.initiated_by, self.admin)

    @override_settings(ADILET_CHECK_ENABLED=False)
    def test_disabled(self):
        check = adilet.check_act(self.act)
        self.assertEqual(check.result, S.NEEDS_CHECK)

    def test_lost_force_notifies_admins(self):
        with mock.patch.object(adilet, "playwright_available", return_value=True), \
                mock.patch.object(adilet, "fetch_page", return_value=(200, URL, HEADER_LOST)):
            adilet.check_act(self.act)
            adilet.check_act(self.act)  # повторно — без дублирования уведомления
        self.act.refresh_from_db()
        self.assertEqual(self.act.status, S.LOST_FORCE)
        self.assertEqual(Notification.objects.filter(user=self.admin, event="legal_act").count(), 1)
        self.assertEqual(Notification.objects.filter(user=self.member).count(), 0)

    def test_fetch_error_unavailable(self):
        with mock.patch.object(adilet, "playwright_available", return_value=True), \
                mock.patch.object(adilet, "fetch_page", side_effect=TimeoutError("timeout")):
            check = adilet.check_act(self.act)
        self.assertEqual(check.result, S.UNAVAILABLE)

    def test_retired_status_kept_and_monthly_skips(self):
        retired = LegalAct.objects.create(commission=self.commission, title="Старый", url=URL, status=S.RETIRED)
        no_url = LegalAct.objects.create(commission=self.commission, title="Без ссылки")
        with mock.patch.object(adilet, "playwright_available", return_value=False):
            result = jobs.run_monthly_legal_check()
        self.assertEqual(result["total"], 1)
        self.assertFalse(LegalActCheck.objects.filter(act__in=[retired, no_url]).exists())
