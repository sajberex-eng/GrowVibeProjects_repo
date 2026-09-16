"""Демонстрационное наполнение: составы 7 основных комиссий, по одному проведённому заседанию
с подписанным протоколом и по одному предстоящему заседанию с утверждённой повесткой.

Прошедшие события проводятся через штатные сервисы (голосование, согласование, подписи, PDF),
при этом «текущее время» временно сдвигается в прошлое. Повторный запуск пропускает комиссии,
у которых уже есть протокол.
"""
import random
from contextlib import contextmanager
from datetime import datetime, time, timedelta
from unittest import mock

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.test.utils import override_settings
from django.utils import timezone

from apps.accounts.models import City, Department, OneTimeCode, Position, User
from apps.committees.models import AbsenceReason, Commission, CompositionChange, MemberRecord, Order, Rubric
from apps.committees.services import register_signed_order
from apps.meetings import services as meeting_services
from apps.meetings.models import AgendaAcknowledgement, AgendaItem, AgendaProposal, Attendance, Invitation, Material, Meeting
from apps.notifications.models import Notification
from apps.protocols import services as protocol_services
from apps.protocols.models import Assignment, Decision, ProtocolApproval, Signature
from apps.voting import services as voting_services
from apps.voting.models import Vote, VotingSession

from .seed_initial import DEMO_PASSWORD

SEED = 20260916

FIRST_NAMES_M = ["Азамат", "Бауыржан", "Дмитрий", "Ербол", "Олжас", "Руслан", "Тимур", "Алексей", "Нуржан", "Максим"]
FIRST_NAMES_F = ["Алия", "Гаухар", "Елена", "Жанна", "Индира", "Камила", "Лаура", "Наталья", "Салтанат", "Татьяна"]
PATRONYMICS_M = ["Асланович", "Болатович", "Викторович", "Ерланович", "Жомартович", "Сергеевич", "Нурланович"]
PATRONYMICS_F = ["Асхатовна", "Бахытовна", "Владимировна", "Ермековна", "Каиртаевна", "Олеговна", "Серикбаевна"]
LAST_NAMES = [
    ("Абенов", "Абенова"), ("Байжанов", "Байжанова"), ("Волков", "Волкова"), ("Данияров", "Даниярова"),
    ("Искаков", "Искакова"), ("Козлов", "Козлова"), ("Мукашев", "Мукашева"), ("Оспанов", "Оспанова"),
    ("Рахимов", "Рахимова"), ("Сарсенов", "Сарсенова"), ("Токтаров", "Токтарова"), ("Шарипов", "Шарипова"),
    ("Федоров", "Федорова"), ("Утепов", "Утепова"), ("Лебедев", "Лебедева"),
]

# Подразделения по должностям (для профилей всех демо-сотрудников).
DEPARTMENTS = {
    "Заместитель директора по медицинской части": "Администрация",
    "Заместитель директора по клинико-экспертной работе": "Администрация",
    "Заведующий отделением гематологии": "Отделение гематологии",
    "Врач-гематолог": "Отделение гематологии",
    "Клинический фармаколог": "Служба клинической фармакологии",
    "Заведующий аптекой": "Аптека",
    "Главная медицинская сестра": "Сестринская служба",
    "Старшая медицинская сестра": "Сестринская служба",
    "Врач-трансфузиолог": "Отделение трансфузиологии",
    "Заведующий отделением трансфузиологии": "Отделение трансфузиологии",
    "Руководитель службы поддержки пациента": "Служба поддержки пациента и внутреннего контроля",
    "Врач-эксперт": "Служба поддержки пациента и внутреннего контроля",
    "Врач-эпидемиолог": "Эпидемиологический отдел",
    "Медицинская сестра-эпидемиолог": "Эпидемиологический отдел",
    "Врач-бактериолог": "Клинико-диагностическая лаборатория",
    "Заведующий лабораторией": "Клинико-диагностическая лаборатория",
    "Специалист по внутреннему аудиту": "Служба поддержки пациента и внутреннего контроля",
    "Юрист": "Юридический отдел",
    "Заведующий отделением ОАРИТ": "Отделение анестезиологии и реанимации",
    "Врач-анестезиолог-реаниматолог": "Отделение анестезиологии и реанимации",
    "Врач-патологоанатом": "Патологоанатомическое бюро",
    "Медицинский психолог": "Отделение гематологии",
    "Врач-методист": "Организационно-методический отдел",
    "Заведующий отделением трансплантации": "Отделение трансплантации костного мозга",
}

# Новые сотрудники: ключ — метка, значение — (пол, должность, город).
NEW_PEOPLE = {
    "trans_head": ("m", "Заведующий отделением трансфузиологии", "Астана"),
    "nurse_senior": ("f", "Старшая медицинская сестра", "Караганда"),
    "hematologist_uk": ("f", "Врач-гематолог", "Усть-Каменогорск"),
    "bacteriologist": ("f", "Врач-бактериолог", "Астана"),
    "nurse_epid": ("f", "Медицинская сестра-эпидемиолог", "Караганда"),
    "lab_head": ("m", "Заведующий лабораторией", "Астана"),
    "vkk_chair": ("m", "Заместитель директора по клинико-экспертной работе", "Астана"),
    "expert": ("f", "Врач-эксперт", "Астана"),
    "hematologist_krg": ("m", "Врач-гематолог", "Караганда"),
    "tkm_head": ("m", "Заведующий отделением трансплантации", "Астана"),
    "pathologist": ("m", "Врач-патологоанатом", "Астана"),
    "methodist": ("f", "Врач-методист", "Астана"),
    "anesthesiologist": ("m", "Врач-анестезиолог-реаниматолог", "Усть-Каменогорск"),
    "ethics_chair": ("m", "Врач-гематолог", "Астана"),
    "psychologist": ("f", "Медицинский психолог", "Астана"),
}

# Составы комиссий без демо-состава. demoNN — существующие пользователи из seed_initial --demo.
COMPOSITIONS = {
    "ЕТС": ("trans_head", "demo08", ["demo02", "demo07", "nurse_senior", "hematologist_uk", "lab_head"]),
    "КИК": ("demo01", "demo10", ["demo07", "demo03", "bacteriologist", "nurse_epid", "lab_head"]),
    "ВКК": ("vkk_chair", "expert", ["demo02", "demo05", "demo14", "hematologist_krg", "tkm_head"]),
    "КИЛИ": ("demo01", "methodist", ["demo02", "demo13", "demo11", "pathologist", "anesthesiologist"]),
    "ЭК": ("ethics_chair", "demo12", ["demo09", "demo07", "psychologist", "demo04", "nurse_senior"]),
}

# Содержание заседаний. Для assignments: (текст, исполнитель-метка роли/логин, срок от сегодня в днях, состояние).
# Состояния: open, progress, overdue, confirm, done, late.
CONTENT = {
    "ЕФК": {
        "past": [
            {
                "title": "Включение препарата венетоклакс в лекарственный формуляр Центра",
                "description": "Заявка отделения гематологии, заключение клинического фармаколога, фармакоэкономический расчёт.",
                "speaker": "demo03", "rubric": "включение в формуляр", "vote": True,
                "discussed": "Обсуждены показания, профиль безопасности и потребность на 2026–2027 гг. Отмечена необходимость контроля синдрома лизиса опухоли.",
                "resolved": "Включить препарат в лекарственный формуляр Центра с ограничением назначения по решению консилиума.",
                "assignments": [
                    ("Внести изменения в лекарственный формуляр и разместить актуальную редакцию", "demo03", 10, "done"),
                    ("Сформировать заявку на закуп на 2027 год", "demo06", 25, "progress"),
                ],
            },
            {
                "title": "Исключение из формуляра препаратов с истёкшей регистрацией",
                "description": "Сверка формуляра с Государственным реестром ЛС и МИ.",
                "speaker": "demo06", "rubric": "исключение из формуляра", "vote": True,
                "discussed": "Выявлено 4 позиции без действующей регистрации; аналоги в формуляре имеются.",
                "resolved": "Исключить 4 позиции из формуляра согласно приложению.",
                "assignments": [
                    ("Проинформировать отделения об исключённых позициях и аналогах", "demo03", -4, "late"),
                ],
            },
            {
                "title": "Мониторинг нежелательных реакций на ЛС за II квартал",
                "description": "Сводный отчёт по извещениям о побочных действиях.",
                "speaker": "demo04", "rubric": "пересмотр формуляра", "vote": False,
                "discussed": "Зарегистрировано 7 извещений, серьёзных реакций — 1. Отмечено недостаточное число извещений из филиалов.",
                "resolved": "Информацию принять к сведению. Усилить работу по фармаконадзору в филиалах.",
                "assignments": [
                    ("Провести обучающий семинар по фармаконадзору для филиалов", "demo05", -3, "overdue"),
                ],
            },
        ],
        "dissent": ("demo05", 1, "Считаю необходимым отложить включение препарата до получения данных о потребности филиалов."),
        "upcoming": [
            ("Пересмотр лекарственного формуляра на 2027 год", "demo03", "пересмотр формуляра", True, "Проект формуляра с учётом заявок отделений."),
            ("Анализ расхода антибактериальных препаратов за III квартал", "demo04", None, False, "Данные аптеки, ATC/DDD-анализ."),
            ("Рассмотрение заявки на включение препарата для профилактики РТПХ", "demo05", "включение в формуляр", True, "Заявка отделения трансплантации."),
        ],
        "proposal": ("demo07", "Порядок хранения ЛС в процедурных кабинетах филиалов", "По итогам проверок выявлены замечания к хранению термолабильных препаратов."),
    },
    "ОСК": {
        "past": [
            {
                "title": "Итоги внутренней экспертизы качества медицинских услуг за II квартал",
                "description": "Проведена экспертиза 120 медицинских карт стационарного пациента.",
                "speaker": "demo11", "rubric": "внутренняя экспертиза качества", "vote": False,
                "discussed": "Доля карт с дефектами оформления — 6,7 %. Основные замечания: неполное обоснование диагноза, отсутствие информированных согласий.",
                "resolved": "Информацию принять к сведению. Заведующим отделениями устранить выявленные дефекты.",
                "assignments": [
                    ("Разработать чек-лист проверки медицинской карты для отделений", "demo11", 14, "progress"),
                    ("Провести разбор дефектов с врачами отделения гематологии", "demo14", 4, "confirm"),
                ],
            },
            {
                "title": "Разбор медицинского инцидента: падение пациента в отделении",
                "description": "Извещение о медицинском инциденте, пациент ID МИС указан в поле вопроса.",
                "speaker": "demo09", "rubric": "медицинские инциденты", "vote": True, "patient_id": "MIS-2026-004512",
                "discussed": "Установлено отсутствие оценки риска падений при поступлении. Вред здоровью не причинён.",
                "resolved": "Признать инцидент предотвратимым. Внедрить шкалу оценки риска падений во всех отделениях.",
                "assignments": [
                    ("Внедрить шкалу Морсе в сестринскую документацию", "demo07", 20, "open"),
                ],
            },
            {
                "title": "Анализ обращений пациентов за июль–август",
                "description": "Сводка обращений, поступивших в службу поддержки пациента.",
                "speaker": "demo09", "rubric": "обращения пациентов", "vote": False,
                "discussed": "Поступило 18 обращений, из них 3 жалобы на сроки ожидания консультации. Все обращения рассмотрены в срок.",
                "resolved": "Информацию принять к сведению. Проработать увеличение слотов записи на консультацию.",
                "assignments": [
                    ("Подготовить предложения по расписанию консультативного приёма", "demo14", 12, "done"),
                ],
            },
        ],
        "upcoming": [
            ("Результаты внешней экспертизы ФСМС за I полугодие", "demo11", "результаты внешней экспертизы", False, "Акты экспертизы и план корректирующих мероприятий."),
            ("Разбор медицинского инцидента: нежелательная реакция на трансфузию", "demo09", "медицинские инциденты", True, "Извещение отделения трансфузиологии.", "MIS-2026-005130"),
            ("Индикаторы качества за III квартал", "demo11", "внутренняя экспертиза качества", False, "Сводные показатели по отделениям."),
        ],
        "proposal": ("demo13", "Оценка боли у пациентов после инвазивных процедур", "Предлагаю включить разбор по результатам анкетирования."),
    },
    "ЕТС": {
        "past": [
            {
                "title": "Анализ использования компонентов крови за I полугодие",
                "description": "Отчёт отделения трансфузиологии: объёмы, списание, возвраты.",
                "speaker": "demo08", "vote": False,
                "discussed": "Списание эритроцитсодержащих компонентов по истечении срока годности — 2,1 %. Основная причина — избыточные заявки.",
                "resolved": "Информацию принять к сведению. Ввести контроль обоснованности заявок на компоненты крови.",
                "assignments": [
                    ("Разработать форму обоснования заявки на компоненты крови", "demo08", 15, "progress"),
                ],
            },
            {
                "title": "Утверждение алгоритма действий при посттрансфузионных реакциях",
                "description": "Проект алгоритма в соответствии со Стандартом оказания трансфузионной помощи.",
                "speaker": "trans_head", "vote": True,
                "discussed": "Предложено дополнить алгоритм порядком информирования организации службы крови.",
                "resolved": "Утвердить алгоритм с учётом внесённых дополнений.",
                "assignments": [
                    ("Довести алгоритм до сведения медицинских сестёр всех отделений под подпись", "demo07", -2, "overdue"),
                    ("Разместить алгоритм в процедурных кабинетах", "nurse_senior", 5, "done"),
                ],
            },
            {
                "title": "Результаты аудита заполнения протоколов трансфузий",
                "description": "Аудит 60 протоколов трансфузий.",
                "speaker": "hematologist_uk", "vote": False,
                "discussed": "В 9 протоколах не указано время начала трансфузии.",
                "resolved": "Провести повторный аудит через 3 месяца.",
                "assignments": [
                    ("Провести повторный аудит протоколов трансфузий", "hematologist_uk", 70, "open"),
                ],
            },
        ],
        "upcoming": [
            ("Отчёт о посттрансфузионных реакциях за III квартал", "demo08", None, False, "Сводные данные отделения трансфузиологии."),
            ("Согласование годовой потребности в компонентах крови на 2027 год", "trans_head", None, True, "Расчёт потребности по отделениям."),
            ("Обучение персонала: правила идентификации пациента перед трансфузией", "nurse_senior", None, False, "План обучения на IV квартал."),
        ],
        "proposal": ("demo02", "Порядок трансфузий в амбулаторных условиях", "Предлагаю рассмотреть регламент дневного стационара."),
    },
    "КИК": {
        "past": [
            {
                "title": "Эпидемиологическая ситуация по ИСМП за II квартал",
                "description": "Отчёт эпидемиолога о случаях инфекций, связанных с оказанием медицинской помощи.",
                "speaker": "demo10", "vote": False,
                "discussed": "Зарегистрировано 3 случая катетер-ассоциированных инфекций кровотока. Проведён эпидемиологический анализ.",
                "resolved": "Информацию принять к сведению. Усилить контроль ухода за центральными венозными катетерами.",
                "assignments": [
                    ("Провести обучение по уходу за ЦВК для медицинских сестёр", "nurse_epid", 8, "progress"),
                ],
            },
            {
                "title": "Результаты микробиологического мониторинга и резистентности",
                "description": "Данные клинико-диагностической лаборатории.",
                "speaker": "bacteriologist", "vote": False,
                "discussed": "Отмечен рост доли карбапенем-резистентных штаммов K. pneumoniae.",
                "resolved": "Пересмотреть схемы эмпирической антибактериальной терапии совместно с ЕФК.",
                "assignments": [
                    ("Подготовить предложения по эмпирической терапии для ЕФК", "demo03", -5, "late"),
                ],
            },
            {
                "title": "Утверждение плана проверок соблюдения санитарно-противоэпидемического режима",
                "description": "План на IV квартал.",
                "speaker": "demo10", "vote": True,
                "discussed": "План согласован с заведующими отделениями.",
                "resolved": "Утвердить план проверок на IV квартал.",
                "assignments": [
                    ("Обеспечить проведение проверок по утверждённому плану", "demo10", 60, "open"),
                    ("Закупить индикаторы контроля стерилизации", "lab_head", -1, "overdue"),
                ],
            },
        ],
        "upcoming": [
            ("Готовность к сезону гриппа и ОРВИ", "demo10", None, False, "Вакцинация персонала, запасы СИЗ."),
            ("Результаты проверок санитарного режима в отделениях", "nurse_epid", None, False, "Акты проверок за квартал."),
            ("Утверждение программы производственного контроля на 2027 год", "demo10", None, True, "Проект программы."),
        ],
        "proposal": ("bacteriologist", "Внедрение экспресс-диагностики MRSA", "Технико-экономическое обоснование прилагается."),
    },
    "ВКК": {
        "past": [
            {
                "title": "Анализ деятельности ВКК за II квартал",
                "description": "Сводные показатели по данным МИС (без персональных данных).",
                "speaker": "expert", "rubric": "заключение ВКК", "vote": False,
                "discussed": "Выдано 214 заключений, из них 38 — о продлении листов временной нетрудоспособности. Нарушений сроков не выявлено.",
                "resolved": "Информацию принять к сведению.",
                "assignments": [
                    ("Подготовить сводный отчёт ВКК за 9 месяцев", "expert", 30, "open"),
                ],
            },
            {
                "title": "Порядок оформления заключений ВКК в МИС",
                "description": "Изменения в шаблонах заключений МИС.",
                "speaker": "vkk_chair", "vote": True,
                "discussed": "Предложено использовать единый шаблон заключения для всех площадок.",
                "resolved": "Утвердить единый шаблон заключения ВКК.",
                "assignments": [
                    ("Направить заявку на изменение шаблона в МИС", "expert", 5, "confirm"),
                ],
            },
            {
                "title": "График работы подкомиссий ВКК на площадках",
                "description": "Астана, Караганда, Усть-Каменогорск.",
                "speaker": "demo02", "vote": True,
                "discussed": "Согласован режим работы подкомиссий в филиалах два раза в неделю.",
                "resolved": "Утвердить график работы подкомиссий ВКК.",
                "assignments": [
                    ("Разместить график на информационных стендах площадок", "hematologist_krg", 3, "done"),
                ],
            },
        ],
        "upcoming": [
            ("Анализ деятельности ВКК за III квартал", "expert", "заключение ВКК", False, "Сводные показатели МИС."),
            ("Изменения в Положении о ВКК", "vkk_chair", None, True, "Проект новой редакции положения."),
            ("Взаимодействие с МСЭ при направлении пациентов", "demo14", None, False, "Порядок и сроки."),
        ],
        "proposal": ("tkm_head", "Порядок ВКК для пациентов после трансплантации", "Предлагаю установить отдельный регламент."),
    },
    "КИЛИ": {
        "past": [
            {
                "title": "Разбор летального исхода в отделении гематологии",
                "description": "Пациент указан ID из МИС. Патологоанатомическое заключение получено.",
                "speaker": "demo02", "rubric": "разбор летального исхода", "vote": True, "patient_id": "MIS-2026-003871",
                "discussed": "Смерть признана непредотвратимой: прогрессирование основного заболевания. Расхождения клинического и патологоанатомического диагнозов нет.",
                "resolved": "Признать летальный исход непредотвратимым. Замечаний к организации медицинской помощи нет.",
                "assignments": [
                    ("Направить заключение КИЛИ в отдел качества", "methodist", -8, "late"),
                ],
            },
            {
                "title": "Разбор летального исхода в отделении реанимации",
                "description": "Пациент указан ID из МИС.",
                "speaker": "demo13", "rubric": "разбор летального исхода", "vote": True, "patient_id": "MIS-2026-004019",
                "discussed": "Выявлена задержка перевода в отделение реанимации на 2 часа. Влияние на исход не установлено.",
                "resolved": "Признать летальный исход условно предотвратимым. Пересмотреть критерии перевода в ОАРИТ.",
                "assignments": [
                    ("Разработать критерии раннего перевода пациентов в ОАРИТ (шкала NEWS2)", "demo13", 21, "progress"),
                    ("Провести клинический разбор со врачами отделений", "anesthesiologist", -2, "overdue"),
                ],
            },
            {
                "title": "Статистика летальности за I полугодие",
                "description": "Сводные данные по отделениям.",
                "speaker": "methodist", "vote": False,
                "discussed": "Показатель летальности сопоставим с прошлым годом.",
                "resolved": "Информацию принять к сведению.",
                "assignments": [],
            },
        ],
        "upcoming": [
            ("Разбор летального исхода в отделении трансплантации", "demo02", "разбор летального исхода", True, "Пациент указан ID из МИС.", "MIS-2026-005402"),
            ("Контроль выполнения рекомендаций КИЛИ", "methodist", None, False, "Отчёт по ранее принятым решениям."),
        ],
        "proposal": ("pathologist", "Сроки предоставления патологоанатомических заключений", "Предлагаю согласовать сроки с отделениями."),
    },
    "ЭК": {
        "past": [
            {
                "title": "Рассмотрение обращения пациента о некорректном поведении персонала",
                "description": "Обращение поступило через службу поддержки пациента.",
                "speaker": "demo09", "vote": True,
                "discussed": "Заслушаны объяснения сотрудника. Факт нарушения этических норм подтверждён частично.",
                "resolved": "Объявить сотруднику замечание; рекомендовать обучение по коммуникативным навыкам.",
                "assignments": [
                    ("Организовать тренинг по коммуникации с пациентами", "psychologist", 18, "open"),
                    ("Направить ответ заявителю", "demo09", -10, "done"),
                ],
            },
            {
                "title": "Декларирование конфликта интересов членами комиссий",
                "description": "Порядок ежегодного декларирования.",
                "speaker": "demo12", "vote": True,
                "discussed": "Предложена единая форма декларации для членов ЕФК и закупочной комиссии.",
                "resolved": "Утвердить форму декларации о конфликте интересов.",
                "assignments": [
                    ("Собрать декларации от членов ЕФК", "demo12", 3, "confirm"),
                ],
            },
            {
                "title": "Актуализация Кодекса этики Центра",
                "description": "Изменения в законодательстве о здоровье народа.",
                "speaker": "ethics_chair", "vote": False,
                "discussed": "Требуется дополнить разделы о цифровой коммуникации и социальных сетях.",
                "resolved": "Подготовить проект новой редакции Кодекса этики.",
                "assignments": [
                    ("Подготовить проект новой редакции Кодекса этики", "demo12", 40, "progress"),
                ],
            },
        ],
        "upcoming": [
            ("Проект новой редакции Кодекса этики Центра", "demo12", None, True, "Проект направлен членам комиссии."),
            ("Итоги анкетирования удовлетворённости персонала", "psychologist", None, False, "Результаты анонимного опроса."),
            ("Рассмотрение обращений за III квартал", "demo09", None, False, "Сводка службы поддержки пациента."),
        ],
        "proposal": ("demo04", "Этические аспекты общения с родственниками пациентов", "Предлагаю разработать памятку."),
    },
}

ORDER = ["ЕТС", "ЕФК", "КИК", "ОСК", "ВКК", "КИЛИ", "ЭК"]


@contextmanager
def frozen_time(moment):
    """Подменяет «текущее время» для сервисов и полей auto_now."""
    with mock.patch("django.utils.timezone.now", return_value=moment):
        yield


def workday(day):
    while day.weekday() >= 5:
        day += timedelta(days=1)
    return day


class Command(BaseCommand):
    help = (
        "Демонстрационное наполнение: составы основных комиссий, по одному проведённому заседанию "
        "с подписанным протоколом и по одному предстоящему заседанию с повесткой."
    )

    def handle(self, *args, **options):
        if not User.objects.filter(username="demo01").exists():
            raise CommandError("Сначала выполните: manage.py seed_initial --demo")
        self.rng = random.Random(SEED)
        self.real_start = timezone.now()
        # Поле по умолчанию хранит ссылку на исходную функцию — делаем его «подменяемым».
        field = Signature._meta.get_field("signed_at")
        field.default = lambda: timezone.now()
        field.__dict__.pop("_get_default", None)

        with override_settings(EMAIL_BACKEND="django.core.mail.backends.dummy.EmailBackend"):
            with transaction.atomic():
                self.admin = User.objects.get(username="admin")
                self.people = self._people()
                self._compositions()
                for index, short_name in enumerate(ORDER):
                    commission = Commission.objects.get(short_name=short_name)
                    if commission.protocols.exists():
                        self.stdout.write(f"{short_name}: протокол уже есть — пропущено")
                        continue
                    protocol = self._past_meeting(commission, CONTENT[short_name], index)
                    meeting = self._upcoming_meeting(commission, CONTENT[short_name], index)
                    self.stdout.write(self.style.SUCCESS(
                        f"{short_name}: протокол № {protocol.number} подписан; "
                        f"предстоящее заседание {timezone.localtime(meeting.starts_at):%d.%m.%Y %H:%M}"
                    ))
                # Уведомления о прошедших событиях считаем прочитанными.
                Notification.objects.filter(
                    created_at__lt=self.real_start - timedelta(hours=1), read_at__isnull=True
                ).update(read_at=self.real_start)
        self.stdout.write(self.style.SUCCESS(
            f"Готово. Пароль всех демо-учётных записей: {DEMO_PASSWORD}"
        ))

    # ------------------------------------------------------------------ люди

    def _people(self):
        people = {u.username: u for u in User.objects.filter(username__startswith="demo")}
        cities = {c.name: c for c in City.objects.all()}
        used = {(u.last_name, u.first_name) for u in User.objects.all()}
        next_num = max(int(name[4:]) for name in people if name[4:].isdigit()) + 1
        for label, (sex, position_name, city_name) in NEW_PEOPLE.items():
            existing = User.objects.filter(email=f"{label}@demo.example.kz").first()
            if existing:
                people[label] = existing
                continue
            while True:
                last = self.rng.choice(LAST_NAMES)[0 if sex == "m" else 1]
                first = self.rng.choice(FIRST_NAMES_M if sex == "m" else FIRST_NAMES_F)
                if (last, first) not in used:
                    used.add((last, first))
                    break
            middle = self.rng.choice(PATRONYMICS_M if sex == "m" else PATRONYMICS_F)
            user = User(
                username=f"demo{next_num:02d}", last_name=last, first_name=first, middle_name=middle,
                email=f"{label}@demo.example.kz", city=cities.get(city_name),
                position=Position.objects.get_or_create(name=position_name)[0], must_change_password=False,
            )
            user.set_password(DEMO_PASSWORD)
            user.save()
            next_num += 1
            people[label] = user
        # Подразделения и телефоны для всех демо-сотрудников.
        for user in set(people.values()):
            changed = False
            if user.position_id and user.position.name in DEPARTMENTS:
                dept = Department.objects.get_or_create(name=DEPARTMENTS[user.position.name])[0]
                if user.department_id != dept.pk:
                    user.department = dept
                    changed = True
            if not user.phone:
                user.phone = f"+7 70{self.rng.randint(0, 8)} {self.rng.randint(100, 999)} {self.rng.randint(10, 99)} {self.rng.randint(10, 99)}"
                changed = True
            if changed:
                user.save()
        return people

    def _person(self, key, commission=None):
        if key in self.people:
            return self.people[key]
        raise CommandError(f"Неизвестный сотрудник: {key}")

    def _compositions(self):
        signed_on = timezone.localdate() - timedelta(days=150)
        for short_name, (chair, secretary, members) in COMPOSITIONS.items():
            commission = Commission.objects.get(short_name=short_name)
            if commission.member_records.exists():
                continue
            order = Order.objects.create(commission=commission, title="О составе комиссии", created_by=self.admin)
            plan = [(chair, MemberRecord.Role.CHAIRMAN), (secretary, MemberRecord.Role.SECRETARY)]
            plan += [(m, MemberRecord.Role.MEMBER) for m in members]
            for key, role in plan:
                CompositionChange.objects.create(
                    commission=commission, action=CompositionChange.Action.ADD, user=self._person(key),
                    new_role=role, basis="Первоначальный состав", order=order, created_by=self.admin,
                )
            register_signed_order(order, f"{signed_on:%m}-{short_name}/од", signed_on)
            self.stdout.write(f"{short_name}: состав из {len(plan)} человек утверждён приказом от {signed_on:%d.%m.%Y}")

    # ------------------------------------------------------------------ прошедшее заседание

    def _at(self, day, hour, minute=0):
        return timezone.make_aware(datetime.combine(day, time(hour, minute)), timezone.get_current_timezone())

    def _past_meeting(self, commission, content, index):
        today = timezone.localdate()
        day = workday(today - timedelta(days=40 - index * 3))
        starts = self._at(day, 14 + index % 3)
        secretary = commission.secretary
        chairman = commission.chairman
        members = list(commission.members_on(day))
        rubrics = {r.name: r for r in Rubric.objects.filter(commission=commission)}

        # Заседание назначено за три недели.
        with frozen_time(starts - timedelta(days=21)):
            meeting = meeting_services.create_meeting(
                Meeting(commission=commission, starts_at=starts, format=Meeting.Format.MIXED,
                        place="Конференц-зал, 2 этаж", video_link="https://meet.example.kz/commission",
                        duration_minutes=90),
                secretary, send_notifications=True, control_item=False,
            )
            meeting.cities.set(commission.cities.all())
            items = []
            for pos, spec in enumerate(content["past"], 1):
                items.append(AgendaItem.objects.create(
                    meeting=meeting, position=pos, title=spec["title"], description=spec["description"],
                    speaker=self._person(spec["speaker"]), duration_minutes=15,
                    rubric=rubrics.get(spec.get("rubric")),
                    patient_id=spec.get("patient_id", "") if commission.handles_patient_cases else "",
                    requires_vote=spec["vote"],
                ))
            meeting_services.submit_agenda(meeting, secretary)
        with frozen_time(starts - timedelta(days=18)):
            meeting_services.approve_agenda(meeting, chairman)
            for rec in members:
                meeting_services.acknowledge(meeting, rec.user, AgendaAcknowledgement.Status.ACK)
            meeting_services.add_material(
                Material(meeting=meeting, agenda_item=items[0], kind=Material.Kind.LINK,
                         title="Справочные материалы к вопросу 1",
                         url="https://portal.example.kz/commissions/materials"),
                secretary, send_notifications=False,
            )

        # День заседания: присутствие, голосование, отметка «проведено».
        with frozen_time(starts + timedelta(hours=2)):
            reason = AbsenceReason.objects.filter(is_excused=True).first()
            values = {}
            for n, rec in enumerate(members):
                if n == len(members) - 1:
                    values[rec.user_id] = (Attendance.Status.ABSENT_EXCUSED, reason)
                elif n % 3 == 2:
                    values[rec.user_id] = (Attendance.Status.ONLINE, None)
                else:
                    values[rec.user_id] = (Attendance.Status.PRESENT, None)
            meeting_services.save_attendance(meeting, values)
            meeting_services.add_material(
                Material(meeting=meeting, kind=Material.Kind.AUDIO, title="Аудиозапись заседания",
                         url="https://records.example.kz/commissions/recording"),
                secretary, send_notifications=False,
            )
            for item in items:
                if not item.requires_vote:
                    continue
                session = VotingSession.objects.create(
                    commission=commission, meeting=meeting, agenda_item=item, question=item.title,
                    description=item.description, deadline=starts + timedelta(days=1), initiated_by=secretary,
                )
                voting_services.open_session(session, secretary)
                voters = list(session.eligible.all())
                for n, voter in enumerate(voters):
                    choice = Vote.Choice.FOR
                    if n == 1 and index % 2 == 0:
                        choice = Vote.Choice.ABSTAIN
                    entered_by = secretary if values.get(voter.pk, ("",))[0] == Attendance.Status.PRESENT and n % 4 == 3 else None
                    voting_services.cast_vote(session, voter, choice, entered_by=entered_by)
            meeting_services.mark_held(meeting, secretary)

        # Черновик протокола на следующий день.
        with frozen_time(starts + timedelta(days=1)):
            protocol = protocol_services.create_draft_from_meeting(meeting, secretary)
            for pitem, spec in zip(protocol.items.order_by("position"), content["past"]):
                pitem.discussed = spec["discussed"]
                pitem.resolved = spec["resolved"]
                pitem.flagged_names = []
                pitem.save()
                decision = Decision.objects.create(
                    protocol=protocol, item=pitem, number=f"{pitem.position}.1", text=spec["resolved"],
                )
                for n, (text, who, due_in, state) in enumerate(spec["assignments"]):
                    assignment = Assignment.objects.create(
                        decision=decision, text=text, responsible=self._person(who),
                        due_date=today + timedelta(days=due_in),
                    )
                    if n == 0 and len(members) > 3:
                        assignment.co_executors.add(members[2].user)
            if commission.handles_patient_cases:
                protocol.patient_ids_confirmed = True
                protocol.save()
            protocol_services.send_to_approval(protocol, secretary)

        # Согласование: все ответили; при наличии — особое мнение.
        with frozen_time(starts + timedelta(days=2, hours=3)):
            dissent = content.get("dissent")
            first_item = protocol.items.order_by("position").first()
            for n, row in enumerate(ProtocolApproval.objects.filter(protocol=protocol, revision=protocol.revision)):
                if n == 1:
                    protocol_services.respond_approval(
                        protocol, row.user, ProtocolApproval.Status.REMARKS,
                        "Предлагаю уточнить формулировку решения по первому вопросу.", first_item,
                    )
                else:
                    protocol_services.respond_approval(protocol, row.user, ProtocolApproval.Status.AGREED)
            if dissent:
                author = self._person(dissent[0])
                item = protocol.items.get(position=dissent[1])
                code = OneTimeCode.issue(author, protocol_services.dissent_purpose(protocol))
                protocol_services.add_dissent(protocol, author, item, dissent[2], None, code)

        # Подписание: секретарь, затем председатель.
        with frozen_time(starts + timedelta(days=3, hours=1)):
            protocol_services.move_to_signing(protocol, secretary)
            self._sign(protocol, secretary)
        with frozen_time(starts + timedelta(days=4, hours=2)):
            protocol.refresh_from_db()
            self._sign(protocol, chairman)

        # Исполнение поручений — в реальном времени.
        for assignment in Assignment.objects.filter(decision__protocol=protocol).select_related("responsible"):
            state = self._state_for(assignment, content)
            self._apply_state(assignment, state, secretary)
        return protocol

    def _sign(self, protocol, user):
        protocol.refresh_from_db()
        code = OneTimeCode.issue(user, protocol_services.sign_purpose(protocol))
        protocol_services.sign(protocol, user, code, ip="10.10.1.15")

    def _state_for(self, assignment, content):
        for spec in content["past"]:
            for text, _who, _due, state in spec["assignments"]:
                if text == assignment.text:
                    return state
        return "open"

    def _apply_state(self, assignment, state, secretary):
        user = assignment.responsible
        if state == "open" or state == "overdue":
            return
        protocol_services.start_assignment(assignment, user)
        if state == "progress":
            protocol_services.add_comment(assignment, user, "Работа ведётся, проект документа на согласовании.")
            return
        assignment.refresh_from_db()
        today = timezone.localdate()
        completed = today if state in ("late", "confirm") else min(today, assignment.due_date)
        protocol_services.report_done(
            assignment, user, "Поручение исполнено, подтверждающие документы направлены секретарю.",
            completed_on=completed,
        )
        if state in ("done", "late"):
            assignment.refresh_from_db()
            protocol_services.confirm_assignment(assignment, secretary, "Исполнение подтверждено.")

    # ------------------------------------------------------------------ предстоящее заседание

    def _upcoming_meeting(self, commission, content, index):
        secretary = commission.secretary
        chairman = commission.chairman
        # Пустые демо-заседания из seed_initial заменяются одним заседанием с повесткой.
        Meeting.objects.filter(
            commission=commission, starts_at__gt=timezone.now(), agenda_items__isnull=True,
        ).delete()
        day = workday(timezone.localdate() + timedelta(days=6 + index * 3))
        starts = self._at(day, 14 + index % 3, 30 if index % 2 else 0)
        meeting = meeting_services.create_meeting(
            Meeting(commission=commission, starts_at=starts, format=Meeting.Format.OFFLINE if index % 2 else Meeting.Format.MIXED,
                    place="Конференц-зал, 2 этаж" if index % 2 else "Зал заседаний, 3 этаж",
                    video_link="" if index % 2 else "https://meet.example.kz/commission",
                    duration_minutes=90, proposals_deadline=starts - timedelta(days=3)),
            secretary, send_notifications=True, control_item=True,
        )
        meeting.cities.set(commission.cities.all())
        rubrics = {r.name: r for r in Rubric.objects.filter(commission=commission)}
        for spec in content["upcoming"]:
            title, speaker, rubric, vote, description = spec[:5]
            patient_id = spec[5] if len(spec) > 5 and commission.handles_patient_cases else ""
            AgendaItem.objects.create(
                meeting=meeting, position=meeting_services.next_position(meeting), title=title,
                description=description, speaker=self._person(speaker), duration_minutes=20,
                rubric=rubrics.get(rubric), patient_id=patient_id, requires_vote=vote,
            )
        meeting_services.renumber(meeting)
        meeting_services.add_material(
            Material(meeting=meeting, kind=Material.Kind.LINK, title="Проект решения и справочные материалы",
                     url="https://portal.example.kz/commissions/upcoming"),
            secretary, send_notifications=False,
        )
        # Предложение члена комиссии в повестку — ждёт рассмотрения секретарём.
        author_key, title, description = content["proposal"]
        meeting_services.create_proposal(
            AgendaProposal(meeting=meeting, title=title, description=description), self._person(author_key),
        )
        meeting_services.submit_agenda(meeting, secretary)
        meeting_services.approve_agenda(meeting, chairman)
        members = list(commission.members_on(meeting.meeting_date))
        for rec in members[: len(members) // 2]:
            meeting_services.acknowledge(meeting, rec.user, AgendaAcknowledgement.Status.ACK)
        if len(members) > 3:
            meeting_services.acknowledge(
                meeting, members[-1].user, AgendaAcknowledgement.Status.SUGGESTION,
                "Прошу увеличить регламент по второму вопросу до 30 минут.",
            )
        # Приглашённый участник из другой комиссии.
        member_ids = {rec.user_id for rec in members}
        guest = User.objects.filter(username__startswith="demo", is_active=True).exclude(pk__in=member_ids).order_by("pk").first()
        if guest:
            meeting_services.add_invitation(
                Invitation(meeting=meeting, user=guest, valid_until=day + timedelta(days=14),
                           note="Докладчик по смежному вопросу"),
                secretary,
            )
        return meeting
