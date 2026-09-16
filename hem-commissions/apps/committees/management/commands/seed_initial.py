"""Начальное наполнение справочников и реестра комиссий (идемпотентно)."""
from datetime import date, datetime, time, timedelta

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from apps.accounts.models import City, Department, Position, User
from apps.committees.models import (
    AbsenceReason, Commission, CompositionChange, Holiday, LegalAct, MemberRecord, Order, Rubric,
)

DEMO_PASSWORD = "Demo-2026-pass"

CITIES = ["Астана", "Караганда", "Усть-Каменогорск"]

ABSENCE_REASONS = [
    ("Отпуск", True),
    ("Больничный", True),
    ("Командировка", True),
    ("Служебная необходимость", True),
    ("Без уважительной причины", False),
]

# Праздничные дни РК на 2026 год. Переносы выходных дней утверждаются постановлением
# Правительства ежегодно — проверяйте и дополняйте календарь в админке.
HOLIDAYS_2026 = [
    (date(2026, 1, 1), "Новый год"),
    (date(2026, 1, 2), "Новый год"),
    (date(2026, 1, 7), "Рождество Христово"),
    (date(2026, 3, 8), "Международный женский день"),
    (date(2026, 3, 21), "Наурыз мейрамы"),
    (date(2026, 3, 22), "Наурыз мейрамы"),
    (date(2026, 3, 23), "Наурыз мейрамы"),
    (date(2026, 5, 1), "Праздник единства народа Казахстана"),
    (date(2026, 5, 7), "День защитника Отечества"),
    (date(2026, 5, 9), "День Победы"),
    (date(2026, 5, 27), "Курбан айт (первый день)"),
    (date(2026, 7, 6), "День Столицы"),
    (date(2026, 8, 30), "День Конституции Республики Казахстан"),
    (date(2026, 10, 25), "День Республики"),
    (date(2026, 12, 16), "День Независимости"),
]

MZ = "МЗ РК"
CHECK_NOTE = "Наименование и реквизиты сверить с adilet.zan.kz"
DSM_230 = {
    "title": "Правила организации и проведения внутренней и внешней экспертиз качества медицинских услуг (помощи)",
    "number": "№ ҚР ДСМ-230/2020", "act_date": date(2020, 12, 3),
    "url": "https://adilet.zan.kz/rus/docs/V2000021727",
}

COMMISSIONS = [
    {
        "short_name": "ЕТС", "name": "Единый трансфузионный совет", "kind": Commission.Kind.CORE,
        "acts": [
            {"title": "Приказ Министра здравоохранения РК в области службы крови", "number": "№ ҚР ДСМ-140/2020",
             "act_date": date(2020, 10, 20), "note": CHECK_NOTE},
            {"title": "Стандарт организации оказания трансфузионной помощи", "number": "(2022)",
             "url": "https://adilet.zan.kz/rus/docs/V2200028571"},
        ],
    },
    {
        "short_name": "ЕФК", "name": "Единая формулярная комиссия", "kind": Commission.Kind.CORE,
        "acts": [
            {"title": "Приказ Министра здравоохранения РК (формулярная система)", "number": "№ ҚР ДСМ-28",
             "act_date": date(2021, 4, 6), "url": "https://adilet.zan.kz/rus/docs/V2100022513", "note": CHECK_NOTE},
            {"title": "Приказ Министра здравоохранения РК (лекарственное обеспечение)", "number": "№ ҚР ДСМ-326/2020",
             "act_date": date(2020, 12, 24), "url": "https://adilet.zan.kz/rus/docs/V2000021913", "note": CHECK_NOTE},
        ],
        "rubrics": ["включение в формуляр", "исключение из формуляра", "пересмотр формуляра"],
    },
    {
        "short_name": "КИК", "name": "Комиссия по инфекционному контролю", "kind": Commission.Kind.CORE,
        "acts": [
            {"title": "Санитарные правила по предупреждению инфекций, связанных с оказанием медицинской помощи (ИСМП)",
             "number": "№ ҚР ДСМ-151", "act_date": date(2022, 12, 2), "note": CHECK_NOTE},
        ],
    },
    {
        "short_name": "ОСК", "name": "Объединённый совет по качеству", "kind": Commission.Kind.CORE,
        "description": (
            "Выполняет функции службы поддержки пациента и внутреннего контроля (СПП и ВК)."
        ),
        "handles_patient_cases": True,
        "acts": [
            DSM_230,
            {"title": "Правила учёта медицинских инцидентов", "number": "№ ҚР ДСМ-147/2020", "note": CHECK_NOTE},
        ],
        "rubrics": ["внутренняя экспертиза качества", "медицинские инциденты", "обращения пациентов",
                    "результаты внешней экспертизы"],
    },
    {
        "short_name": "ВКК", "name": "Врачебно-консультативная комиссия", "kind": Commission.Kind.CORE,
        "description": "Данные пациентов по заключениям ВКК ведутся в МИС.",
        "handles_patient_cases": False,
        "acts": [
            {"title": "Приказ Министра здравоохранения РК о деятельности врачебно-консультативной комиссии",
             "number": "№ ҚР ДСМ-34", "act_date": date(2022, 4, 7),
             "url": "https://adilet.zan.kz/rus/docs/V2200027505", "note": CHECK_NOTE},
        ],
        "rubrics": ["заключение ВКК"],
    },
    {
        "short_name": "КИЛИ", "name": "Комиссия по изучению летальных исходов", "kind": Commission.Kind.CORE,
        "handles_patient_cases": True,
        "acts": [DSM_230],
        "rubrics": ["разбор летального исхода"],
    },
    {
        "short_name": "ЭК", "name": "Этическая комиссия", "kind": Commission.Kind.CORE,
        "acts": [
            DSM_230,
            {"title": "Кодекс Республики Казахстан «О здоровье народа и системе здравоохранения»",
             "authority": "Парламент РК"},
        ],
    },
    {
        "short_name": "ЛЭК", "name": "Локальная этическая комиссия", "kind": Commission.Kind.REQUIRED_IF,
        "is_active": False,
        "description": "Создаётся при проведении клинических исследований.",
    },
    {
        "short_name": "НСПВ", "name": "Комиссия по контролю оборота наркотических средств и психотропных веществ",
        "kind": Commission.Kind.RECOMMENDED, "is_active": False,
        "acts": [
            {"title": "Приказ об обороте наркотических средств, психотропных веществ и прекурсоров",
             "number": "№ 32", "url": "https://adilet.zan.kz/rus/docs/V1500010404", "note": CHECK_NOTE},
        ],
    },
    {
        "short_name": "АМП", "name": "Комиссия по рациональному использованию антимикробных препаратов",
        "kind": Commission.Kind.RECOMMENDED, "is_active": False,
    },
    {
        "short_name": "РМИ", "name": "Комиссия по разбору медицинских инцидентов",
        "kind": Commission.Kind.RECOMMENDED, "is_active": False,
    },
    {
        "short_name": "ССД", "name": "Совет по сестринскому делу",
        "kind": Commission.Kind.RECOMMENDED, "is_active": False,
    },
    {
        "short_name": "ОТ", "name": "Комиссия по охране труда и расследованию несчастных случаев",
        "kind": Commission.Kind.RECOMMENDED, "is_active": False,
    },
    {
        "short_name": "ЗЛС", "name": "Комиссия по закупу лекарственных средств и медицинских изделий",
        "kind": Commission.Kind.RECOMMENDED, "is_active": False,
    },
]

# Вымышленные сотрудники для демонстрации.
DEMO_PEOPLE = [
    ("Ахметова", "Динара", "Сериковна", "Заместитель директора по медицинской части"),
    ("Сейтказиев", "Ерлан", "Маратович", "Заведующий отделением гематологии"),
    ("Иванова", "Ольга", "Петровна", "Клинический фармаколог"),
    ("Нурланова", "Айгерим", "Бекетовна", "Врач-гематолог"),
    ("Ким", "Виктор", "Андреевич", "Врач-гематолог"),
    ("Жумабеков", "Арман", "Талгатович", "Заведующий аптекой"),
    ("Смагулова", "Жанар", "Кайратовна", "Главная медицинская сестра"),
    ("Орлов", "Сергей", "Николаевич", "Врач-трансфузиолог"),
    ("Тулегенова", "Асель", "Муратовна", "Руководитель службы поддержки пациента"),
    ("Бекмуханов", "Данияр", "Ерланович", "Врач-эпидемиолог"),
    ("Абдрахманова", "Сауле", "Жанатовна", "Специалист по внутреннему аудиту"),
    ("Петренко", "Мария", "Игоревна", "Юрист"),
    ("Касымов", "Нурлан", "Айдарович", "Заведующий отделением ОАРИТ"),
    ("Есенова", "Гульмира", "Сабитовна", "Врач-гематолог"),
]
DEMO_COMPOSITION = {
    # индексы DEMO_PEOPLE: председатель, секретарь, члены
    "ЕФК": (0, 2, [1, 3, 4, 5, 6]),
    "ОСК": (0, 10, [8, 9, 11, 12, 13]),
}


class Command(BaseCommand):
    help = (
        "Начальные справочники: города, причины отсутствия, праздники РК на 2026 г., реестр комиссий, "
        "НПА и рубрики. Повторный запуск безопасен. ВНИМАНИЕ: переносы выходных дней утверждаются "
        "Правительством РК ежегодно — сверяйте производственный календарь (Курбан айт — по лунному "
        "календарю) и дополняйте его в админке. --demo добавляет демо-пользователей, составы ЕФК и ОСК "
        f"и заседания; пароль всех демо-учётных записей: {DEMO_PASSWORD}."
    )

    def add_arguments(self, parser):
        parser.add_argument("--demo", action="store_true", help="Создать демонстрационные данные")

    @transaction.atomic
    def handle(self, *args, **options):
        cities = {name: City.objects.get_or_create(name=name)[0] for name in CITIES}
        for name, excused in ABSENCE_REASONS:
            AbsenceReason.objects.get_or_create(name=name, defaults={"is_excused": excused})
        for day, name in HOLIDAYS_2026:
            Holiday.objects.get_or_create(date=day, defaults={"name": name})

        created = 0
        for spec in COMMISSIONS:
            commission, was_created = Commission.objects.get_or_create(
                short_name=spec["short_name"],
                defaults={
                    "name": spec["name"],
                    "kind": spec["kind"],
                    "description": spec.get("description", ""),
                    "is_active": spec.get("is_active", True),
                    "handles_patient_cases": spec.get("handles_patient_cases", False),
                },
            )
            if was_created:
                created += 1
                commission.cities.set(cities.values())
            for i, act in enumerate(spec.get("acts", []), 1):
                LegalAct.objects.get_or_create(
                    commission=commission, title=act["title"], number=act.get("number", ""),
                    defaults={
                        "act_date": act.get("act_date"),
                        "authority": act.get("authority", MZ),
                        "url": act.get("url", ""),
                        "note": act.get("note", ""),
                        "status": LegalAct.Status.NEEDS_CHECK,
                        "sort": i * 10,
                    },
                )
            for i, name in enumerate(spec.get("rubrics", []), 1):
                Rubric.objects.get_or_create(commission=commission, name=name, defaults={"sort": i * 10})

        self.stdout.write(self.style.SUCCESS(
            f"Справочники готовы. Комиссий создано: {created}, всего: {Commission.objects.count()}."
        ))
        self.stdout.write(
            "Проверьте производственный календарь: переносы выходных дней утверждаются Правительством ежегодно."
        )
        if options["demo"]:
            self._demo(cities)

    # ---------- демо ----------

    def _demo(self, cities):
        admin, created = User.objects.get_or_create(
            username="admin",
            defaults={"first_name": "Администратор", "last_name": "Системы", "email": "admin@example.kz",
                      "is_staff": True, "is_superuser": True, "must_change_password": False},
        )
        if created:
            admin.set_password(DEMO_PASSWORD)
            admin.save()
        department, _ = Department.objects.get_or_create(name="Администрация")
        city_list = list(cities.values())
        people = []
        for i, (last, first, middle, position_name) in enumerate(DEMO_PEOPLE):
            position, _ = Position.objects.get_or_create(name=position_name)
            user, was_created = User.objects.get_or_create(
                username=f"demo{i + 1:02d}",
                defaults={
                    "last_name": last, "first_name": first, "middle_name": middle,
                    "email": f"demo{i + 1:02d}@example.kz", "position": position, "department": department,
                    "city": city_list[i % len(city_list)], "must_change_password": False,
                },
            )
            if was_created:
                user.set_password(DEMO_PASSWORD)
                user.save()
            people.append(user)

        from apps.committees.services import register_signed_order

        signed_on = timezone.localdate() - timedelta(days=60)
        for short_name, (chair, secretary, members) in DEMO_COMPOSITION.items():
            commission = Commission.objects.get(short_name=short_name)
            if commission.member_records.exists():
                continue
            order = Order.objects.create(
                commission=commission, title="О составе комиссии", status=Order.Status.DRAFT, created_by=admin,
            )
            plan = [(chair, MemberRecord.Role.CHAIRMAN), (secretary, MemberRecord.Role.SECRETARY)]
            plan += [(m, MemberRecord.Role.MEMBER) for m in members]
            for idx, role in plan:
                CompositionChange.objects.create(
                    commission=commission, action=CompositionChange.Action.ADD, user=people[idx],
                    new_role=role, basis="Первоначальный состав", order=order, created_by=admin,
                )
            register_signed_order(order, f"{signed_on:%m}-{short_name}/од", signed_on)

        from apps.meetings.models import Meeting

        tz = timezone.get_current_timezone()
        for short_name in DEMO_COMPOSITION:
            commission = Commission.objects.get(short_name=short_name)
            if Meeting.objects.filter(commission=commission, starts_at__gte=timezone.now()).exists():
                continue
            for days, place in ((7, "Конференц-зал, 2 этаж"), (28, "Конференц-зал, 2 этаж")):
                day = timezone.localdate() + timedelta(days=days)
                while day.weekday() >= 5:
                    day += timedelta(days=1)
                Meeting.objects.create(
                    commission=commission,
                    starts_at=timezone.make_aware(datetime.combine(day, time(15, 0)), tz),
                    place=place, created_by=admin,
                )
        self.stdout.write(self.style.SUCCESS(
            "Демо-данные готовы. Вход: admin (администратор), demo01…demo14 (сотрудники). "
            f"Пароль для всех демо-учётных записей: {DEMO_PASSWORD}"
        ))
