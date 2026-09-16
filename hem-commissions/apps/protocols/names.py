"""Поиск вероятных ФИО (пациентов) в тексте протокола.

Эвристика для комиссий, рассматривающих клинические случаи: в протоколе
допускается только ID пациента из МИС. Ищутся:
  * «Фамилия Имя Отчество» — три слова с заглавной буквы, третье похоже на отчество;
  * «Фамилия И.О.» и «И.О. Фамилия».
ФИО пользователей системы (члены комиссий, докладчики и т. п.) исключаются.
"""
import re

UP = "А-ЯЁӘҒҚҢӨҰҮҺІ"
LO = "а-яёәғқңөұүһі"
WORD = rf"[{UP}][{LO}]+(?:-[{UP}][{LO}]+)?"
PATRONYMIC = rf"[{UP}][{LO}]+(?:вич|вна|ична|чна|вича|вичу|вичем|вне|вну|вной|ичны|ичне|ичну|ичной|улы|уулу|кызы|қызы|оглы)"

FULL_RE = re.compile(rf"(?<![{UP}{LO}])({WORD})\s+({WORD})\s+({PATRONYMIC})(?![{UP}{LO}])")
INITIALS_AFTER_RE = re.compile(rf"(?<![{UP}{LO}])({WORD})\s+([{UP}])\.\s?([{UP}])\.")
INITIALS_BEFORE_RE = re.compile(rf"(?<![{UP}{LO}.])([{UP}])\.\s?([{UP}])\.\s?({WORD})(?![{UP}{LO}])")


def _same_word(a, b):
    """Сравнение с учётом падежных окончаний: Иванова ~ Иванов."""
    a, b = a.lower(), b.lower()
    if a == b:
        return True
    p = min(len(a), len(b))
    if p < 3 or abs(len(a) - len(b)) > 3:
        return False
    return a[: p - 1] == b[: p - 1]


class KnownNames:
    def __init__(self, people):
        """people — итерируемое из кортежей (фамилия, имя, отчество)."""
        self.people = [tuple((part or "").strip() for part in person) for person in people if person and person[0]]

    def matches_full(self, last, first, middle):
        for p_last, p_first, p_middle in self.people:
            if _same_word(last, p_last) and p_first and _same_word(first, p_first):
                if not p_middle or _same_word(middle, p_middle):
                    return True
        return False

    def matches_initials(self, last, i1, i2):
        for p_last, p_first, p_middle in self.people:
            if _same_word(last, p_last) and p_first[:1] == i1 and (not p_middle or p_middle[:1] == i2):
                return True
        return False


def known_names_from_system(extra_names=()):
    """ФИО всех пользователей системы + дополнительные строки «Фамилия Имя Отчество»."""
    from apps.accounts.models import User

    people = list(User.objects.values_list("last_name", "first_name", "middle_name"))
    for name in extra_names:
        parts = (name or "").replace(".", ". ").split()
        if len(parts) >= 2:
            people.append((parts[0], parts[1].rstrip("."), (parts[2].rstrip(".") if len(parts) > 2 else "")))
    return KnownNames(people)


def find_person_names(text, known=None):
    """Возвращает список найденных вероятных ФИО (без дубликатов, в порядке появления)."""
    if not text:
        return []
    found = []

    def add(value):
        value = re.sub(r"\s+", " ", value).strip()
        if value not in found:
            found.append(value)

    for m in FULL_RE.finditer(text):
        last, first, middle = m.groups()
        if known and known.matches_full(last, first, middle):
            continue
        add(m.group(0))
    taken = []
    for m in INITIALS_AFTER_RE.finditer(text):
        taken.append(m.span())
        last, i1, i2 = m.groups()
        if known and known.matches_initials(last, i1, i2):
            continue
        add(m.group(0))
    for m in INITIALS_BEFORE_RE.finditer(text):
        if any(start <= m.start() < end for start, end in taken):
            continue  # «Фамилия И.О. Слово» — инициалы уже относятся к фамилии слева
        i1, i2, last = m.groups()
        if known and known.matches_initials(last, i1, i2):
            continue
        add(m.group(0))
    return found


def flag_item(item, known=None):
    """Заполняет item.flagged_names по всем текстовым полям вопроса (без сохранения)."""
    known = known or known_names_from_system()
    text = "\n".join([item.title or "", item.heard or "", item.discussed or "", item.resolved or ""])
    item.flagged_names = find_person_names(text, known)
    return item.flagged_names
