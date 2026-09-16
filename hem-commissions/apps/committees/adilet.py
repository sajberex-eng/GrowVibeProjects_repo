"""Проверка актуальности НПА на adilet.zan.kz.

Страницы adilet.zan.kz формируются JavaScript, поэтому текст получаем через
Playwright (Chromium, headless). Playwright — необязательная зависимость:
  pip install playwright && playwright install chromium
Без него проверка возвращает «Требует проверки».

Разбор страницы вынесен в небольшие функции (`classify_page` и списки шаблонов),
чтобы их было легко поправить при изменении вёрстки портала.
Правило: при любой неопределённости результат — «Требует проверки», но не «Актуален».
"""
import logging
import re

from django.conf import settings
from django.utils import timezone

from .models import LegalAct, LegalActCheck

logger = logging.getLogger(__name__)

S = LegalAct.Status

# Сколько символов с начала страницы считать «шапкой» (реквизиты и статус документа).
HEAD_CHARS = 2500

_CYR = "а-яёәғқңөұүһі"

LOST_FORCE_PATTERNS = [
    r"утратил[аио]?\s+силу",
    r"утративш(ий|ая|ее|ие)\s+силу",
    r"(?<![" + _CYR + r"])не\s+действует",
    r"(?<![" + _CYR + r"])недействующий",
    r"күшін\s+жойған",
]
ACTUAL_PATTERNS = [
    r"(?<![" + _CYR + r"])(?<!не )действующий(?![" + _CYR + r"])",
    r"(?<![" + _CYR + r"])жаңа(?![" + _CYR + r"])",
    r"қолданыстағы",
]
NOT_FOUND_PATTERNS = [
    r"страница\s+не\s+найдена",
    r"документ\s+не\s+найден",
    r"page\s+not\s+found",
    r"404\s+not\s+found",
]


def _find(patterns, text):
    for pattern in patterns:
        m = re.search(pattern, text, flags=re.IGNORECASE)
        if m:
            return m.group(0)
    return None


def _normalize(text):
    return re.sub(r"\s+", " ", (text or "").replace("\xa0", " ")).strip().lower()


def classify_page(status_code, final_url, text):
    """Определяет статус НПА по результату загрузки страницы.

    Возвращает (статус LegalAct.Status, пояснение).
    """
    if status_code is None:
        return S.UNAVAILABLE, "Страница не загрузилась (нет ответа сервера)."
    if status_code >= 400:
        return S.UNAVAILABLE, f"Сервер вернул HTTP {status_code}."
    final_url = final_url or ""
    if "adilet.zan.kz" in final_url and "/docs/" not in final_url:
        return S.UNAVAILABLE, f"Перенаправление со страницы документа на {final_url}."
    body = _normalize(text)
    if not body:
        return S.UNAVAILABLE, "Страница пустая."
    head = body[:HEAD_CHARS]
    if _find(NOT_FOUND_PATTERNS, head):
        return S.UNAVAILABLE, "Портал сообщает, что документ не найден."

    lost = _find(LOST_FORCE_PATTERNS, head)
    actual = _find(ACTUAL_PATTERNS, head)
    if lost and actual:
        return S.NEEDS_CHECK, (
            f"В реквизитах одновременно встречаются «{actual}» и «{lost}» — проверьте вручную."
        )
    if lost:
        return S.LOST_FORCE, f"В реквизитах документа: «{lost}»."
    if actual:
        return S.ACTUAL, f"Статус документа: «{actual}»."
    body_lost = _find(LOST_FORCE_PATTERNS, body)
    if body_lost:
        return S.NEEDS_CHECK, (
            f"Статус не распознан; в тексте встречается «{body_lost}» (может относиться к отдельным нормам)."
        )
    return S.NEEDS_CHECK, "Статус документа на странице не распознан."


class PlaywrightMissing(RuntimeError):
    pass


def playwright_available():
    try:
        import playwright.sync_api  # noqa: F401
    except ImportError:
        return False
    return True


def fetch_page(url, timeout=None):
    """Загружает страницу браузером. Возвращает (status_code, final_url, text)."""
    if not playwright_available():
        raise PlaywrightMissing("Playwright не установлен")
    from playwright.sync_api import sync_playwright

    timeout_ms = int((timeout or settings.ADILET_TIMEOUT_SECONDS) * 1000)
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            page = browser.new_page(locale="ru-RU")
            response = page.goto(url, wait_until="networkidle", timeout=timeout_ms)
            status = response.status if response else None
            try:
                page.wait_for_timeout(1500)
            except Exception:  # noqa: BLE001
                pass
            text = page.inner_text("body")
            return status, page.url, text
        finally:
            browser.close()


def check_act(act, manual=False, user=None):
    """Проверяет один НПА, пишет журнал, обновляет статус. Возвращает LegalActCheck."""
    previous = act.status
    if not act.url:
        result, details = S.NEEDS_CHECK, "Не указана ссылка на adilet.zan.kz."
    elif not settings.ADILET_CHECK_ENABLED:
        result, details = S.NEEDS_CHECK, "Автоматическая проверка отключена в настройках (ADILET_CHECK_ENABLED)."
    elif not playwright_available():
        result, details = S.NEEDS_CHECK, "Playwright не установлен — автоматическая проверка невозможна."
    else:
        try:
            status_code, final_url, text = fetch_page(act.url)
            result, details = classify_page(status_code, final_url, text)
        except PlaywrightMissing:
            result, details = S.NEEDS_CHECK, "Playwright не установлен — автоматическая проверка невозможна."
        except Exception as exc:  # noqa: BLE001 — сетевые ошибки и таймауты
            logger.warning("adilet check failed for act %s: %s", act.pk, exc)
            result, details = S.UNAVAILABLE, f"Ошибка загрузки страницы: {str(exc)[:300]}"

    check = LegalActCheck.objects.create(
        act=act, url=act.url, result=result, details=details, manual=manual,
        initiated_by=user if user is not None and getattr(user, "pk", None) else None,
    )
    act.last_checked_at = check.checked_at or timezone.now()
    if act.status != S.RETIRED:
        act.status = result
    act.save(update_fields=["status", "last_checked_at"])

    if result in (S.LOST_FORCE, S.UNAVAILABLE) and previous != result and act.status != S.RETIRED:
        from django.urls import reverse

        from apps.notifications.models import Event
        from apps.notifications.services import notify_admins

        label = LegalAct.Status(result).label
        notify_admins(
            Event.LEGAL_ACT,
            f"НПА: {label.lower()} — {act.commission.short_name}",
            f"{act}\nКомиссия: {act.commission.name}\nРезультат проверки: {label}. {details}",
            reverse("committees:detail", args=[act.commission_id]) + "?tab=legal",
        )
    return check


def check_acts(acts, manual=False, user=None):
    """Проверяет набор НПА. Возвращает счётчики по результатам."""
    counts = {"total": 0}
    for act in acts:
        check = check_act(act, manual=manual, user=user)
        counts["total"] += 1
        counts[check.result] = counts.get(check.result, 0) + 1
    return counts


def checkable_acts(commission=None):
    qs = LegalAct.objects.exclude(status=S.RETIRED).exclude(url="").select_related("commission")
    if commission is not None:
        qs = qs.filter(commission=commission)
    return qs
