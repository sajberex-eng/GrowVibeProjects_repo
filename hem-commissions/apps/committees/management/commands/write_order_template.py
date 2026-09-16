from django.core.management.base import BaseCommand

from apps.committees.docgen import write_sample_order_template


class Command(BaseCommand):
    help = (
        "Создаёт образец DOCX-шаблона приказа о составе с плейсхолдерами docxtpl "
        "(по умолчанию docs/templates/order_template.docx). Отредактируйте его в Word и загрузите "
        "в «Справочники и шаблоны → Шаблоны документов» (вид «Приказ о составе»)."
    )

    def add_arguments(self, parser):
        parser.add_argument("--path", default=None, help="Куда сохранить файл")

    def handle(self, *args, **options):
        path = write_sample_order_template(options["path"])
        self.stdout.write(self.style.SUCCESS(f"Шаблон сохранён: {path}"))
