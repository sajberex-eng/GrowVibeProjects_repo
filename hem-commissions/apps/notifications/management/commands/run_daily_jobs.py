import json

from django.core.management.base import BaseCommand
from django.utils import timezone

from apps.notifications import scheduler


class Command(BaseCommand):
    help = "Запускает ежедневные задачи (напоминания, просрочки, закрытие голосований); --monthly — и ежемесячные."

    def add_arguments(self, parser):
        parser.add_argument("--monthly", action="store_true", help="Также выполнить ежемесячную проверку НПА")

    def handle(self, *args, **options):
        now = timezone.now()
        summary = {"daily": scheduler.run_daily(now)}
        scheduler.mark_run("daily", now, summary["daily"])
        if options["monthly"]:
            summary["monthly"] = scheduler.run_monthly(now)
            scheduler.mark_run("monthly", now, summary["monthly"])
        self.stdout.write(json.dumps(summary, ensure_ascii=False, indent=2, default=str))
