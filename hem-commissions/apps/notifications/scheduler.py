"""Планировщик периодических задач.

Каждое приложение предоставляет модуль ``jobs`` с функцией ``run(now=None) -> dict``.
Сбой или отсутствие одного модуля не мешает выполнению остальных.
"""
import importlib
import json
import logging

from django.utils import timezone

from .models import JobRun

logger = logging.getLogger(__name__)

DAILY_JOBS = [
    ("apps.committees.jobs", "run"),
    ("apps.meetings.jobs", "run"),
    ("apps.protocols.jobs", "run"),
    ("apps.voting.jobs", "run"),
]
MONTHLY_JOBS = [("apps.committees.jobs", "run_monthly_legal_check")]
FREQUENT_JOBS = [("apps.voting.jobs", "run")]

DAILY_HOUR = 7


def _plain(value):
    try:
        json.dumps(value)
        return value
    except (TypeError, ValueError):
        return str(value)


def call_job(module_path, func_name, now=None):
    key = f"{module_path}.{func_name}"
    try:
        module = importlib.import_module(module_path)
    except ModuleNotFoundError as exc:
        if exc.name and module_path.startswith(exc.name):
            logger.warning("Модуль задач %s не найден — пропущено", module_path)
            return key, {"skipped": "module not found"}
        logger.exception("Ошибка импорта %s", module_path)
        return key, {"error": str(exc)}
    except Exception as exc:  # noqa: BLE001
        logger.exception("Ошибка импорта %s", module_path)
        return key, {"error": str(exc)}
    func = getattr(module, func_name, None)
    if func is None:
        logger.warning("В модуле %s нет функции %s — пропущено", module_path, func_name)
        return key, {"skipped": "function not found"}
    try:
        result = func(now=now)
    except Exception as exc:  # noqa: BLE001 — одна упавшая задача не останавливает остальные
        logger.exception("Задача %s завершилась с ошибкой", key)
        return key, {"error": str(exc)}
    return key, _plain(result if result is not None else {})


def _run_all(jobs, now):
    return dict(call_job(module, func, now=now) for module, func in jobs)


def run_daily(now=None):
    return _run_all(DAILY_JOBS, now)


def run_monthly(now=None):
    return _run_all(MONTHLY_JOBS, now)


def run_frequent(now=None):
    return _run_all(FREQUENT_JOBS, now)


def mark_run(name, now, result):
    JobRun.objects.update_or_create(name=name, defaults={"last_run": now, "last_result": _plain(result)})


def _last_run(name):
    job = JobRun.objects.filter(name=name).first()
    return job.last_run if job else None


def tick(now=None):
    """Один проход планировщика: частые задачи всегда, ежедневные — раз в сутки
    после 07:00 местного времени, ежемесячные — 1-го числа."""
    now = now or timezone.now()
    local = timezone.localtime(now)
    today = local.date()
    summary = {"frequent": run_frequent(now)}
    mark_run("frequent", now, summary["frequent"])

    last_daily = _last_run("daily")
    if local.hour >= DAILY_HOUR and (last_daily is None or timezone.localtime(last_daily).date() < today):
        summary["daily"] = run_daily(now)
        mark_run("daily", now, summary["daily"])

    last_monthly = _last_run("monthly")
    if today.day == 1 and local.hour >= DAILY_HOUR and (
        last_monthly is None or timezone.localtime(last_monthly).date() < today
    ):
        summary["monthly"] = run_monthly(now)
        mark_run("monthly", now, summary["monthly"])
    return summary
