import json
import logging
import time

from django.core.management.base import BaseCommand
from django.db import close_old_connections

from apps.notifications import scheduler

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = (
        "Бесконечный цикл планировщика (контейнер worker): каждые N минут закрывает истёкшие голосования, "
        "раз в сутки после 07:00 — ежедневные задачи, 1-го числа — ежемесячная проверка НПА."
    )

    def add_arguments(self, parser):
        parser.add_argument("--interval", type=int, default=15, help="Интервал, минут (по умолчанию 15)")
        parser.add_argument("--once", action="store_true", help="Выполнить один проход и выйти")

    def handle(self, *args, **options):
        interval = max(1, options["interval"]) * 60
        self.stdout.write(f"Планировщик запущен, интервал {interval // 60} мин.")
        while True:
            close_old_connections()
            try:
                summary = scheduler.tick()
                self.stdout.write(json.dumps(summary, ensure_ascii=False, default=str))
            except Exception:  # noqa: BLE001 — цикл не должен падать
                logger.exception("Ошибка прохода планировщика")
            if options["once"]:
                break
            time.sleep(interval)
