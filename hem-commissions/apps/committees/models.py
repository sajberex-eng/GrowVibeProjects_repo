import uuid
from datetime import date
from fractions import Fraction
from math import floor

from django.conf import settings
from django.core.validators import MinValueValidator
from django.db import models
from django.db.models import Q
from django.utils import timezone

from apps.accounts.models import City


def upload_path(instance, filename):
    """Путь в хранилище без исходного имени (исходное хранится отдельно)."""
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else "bin"
    today = timezone.localdate()
    return f"{instance._meta.model_name}/{today:%Y/%m}/{uuid.uuid4().hex}.{ext}"


class Holiday(models.Model):
    """Производственный календарь РК: праздники и переносы для расчёта рабочих дней."""

    date = models.DateField("Дата", unique=True)
    name = models.CharField("Наименование", max_length=200, blank=True)
    is_working_day = models.BooleanField(
        "Рабочий день (перенос)", default=False,
        help_text="Отметьте, если выходной день перенесён и является рабочим.",
    )

    class Meta:
        verbose_name = "День производственного календаря"
        verbose_name_plural = "Производственный календарь"
        ordering = ["date"]

    def __str__(self):
        return f"{self.date:%d.%m.%Y} {self.name}"


class Commission(models.Model):
    class Kind(models.TextChoices):
        CORE = "core", "Основная (на старте)"
        REQUIRED_IF = "required_if", "Требуется НПА при наличии деятельности"
        RECOMMENDED = "recommended", "Создаётся приказом руководителя"

    name = models.CharField("Полное наименование", max_length=300, unique=True)
    short_name = models.CharField("Краткое наименование", max_length=20, unique=True)
    kind = models.CharField("Тип", max_length=20, choices=Kind.choices, default=Kind.CORE)
    description = models.TextField("Назначение", blank=True)
    established_on = models.DateField("Дата создания", null=True, blank=True)
    cities = models.ManyToManyField(City, verbose_name="Площадки", blank=True)
    is_active = models.BooleanField("Действует", default=True)
    handles_patient_cases = models.BooleanField(
        "Рассматривает клинические случаи", default=False,
        help_text="В вопросах повестки указывается только внутренний ID пациента из МИС.",
    )

    # Настройки
    meeting_quorum_numerator = models.PositiveSmallIntegerField("Кворум заседания: числитель", default=1)
    meeting_quorum_denominator = models.PositiveSmallIntegerField(
        "Кворум заседания: знаменатель", default=2, validators=[MinValueValidator(1)]
    )
    meeting_quorum_strict = models.BooleanField(
        "Кворум заседания: строго больше доли", default=True,
        help_text="Включено — «более половины»; выключено — «не менее».",
    )
    vote_quorum_numerator = models.PositiveSmallIntegerField("Кворум голосования: числитель", default=2)
    vote_quorum_denominator = models.PositiveSmallIntegerField(
        "Кворум голосования: знаменатель", default=3, validators=[MinValueValidator(1)]
    )
    approval_days = models.PositiveSmallIntegerField("Срок согласования протокола, рабочих дней", default=3)
    vote_days = models.PositiveSmallIntegerField("Срок голосования по умолчанию, рабочих дней", default=3)
    open_voting = models.BooleanField("Поимённые результаты голосования видны членам", default=True)
    allow_vote_change = models.BooleanField("Разрешено менять голос до окончания срока", default=True)
    whatsapp_enabled = models.BooleanField("Уведомления в WhatsApp", default=False)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Комиссия"
        verbose_name_plural = "Комиссии"
        ordering = ["kind", "name"]

    def __str__(self):
        return self.short_name

    # --- состав ---
    def members_on(self, on_date=None):
        """Действующий состав на дату (по подписанным приказам)."""
        on_date = on_date or timezone.localdate()
        return (
            self.member_records.filter(start_date__lte=on_date)
            .filter(Q(end_date__isnull=True) | Q(end_date__gt=on_date))
            .select_related("user", "user__city", "user__position")
            .order_by("role_order", "user__last_name")
        )

    def member_users_on(self, on_date=None):
        from apps.accounts.models import User

        ids = self.members_on(on_date).values_list("user_id", flat=True)
        return User.objects.filter(pk__in=list(ids))

    def officer(self, role, on_date=None):
        rec = self.members_on(on_date).filter(role=role).first()
        return rec.user if rec else None

    @property
    def chairman(self):
        return self.officer(MemberRecord.Role.CHAIRMAN)

    @property
    def secretary(self):
        return self.officer(MemberRecord.Role.SECRETARY)

    # --- кворум ---
    def meeting_quorum_needed(self, total):
        share = Fraction(self.meeting_quorum_numerator, self.meeting_quorum_denominator)
        threshold = share * total
        if self.meeting_quorum_strict:
            return floor(threshold) + 1
        return -(-threshold.numerator // threshold.denominator)  # ceil

    def vote_quorum_needed(self, total):
        threshold = Fraction(self.vote_quorum_numerator, self.vote_quorum_denominator) * total
        return -(-threshold.numerator // threshold.denominator)  # ceil: «не менее 2/3»

    @property
    def meeting_quorum_label(self):
        prefix = "более" if self.meeting_quorum_strict else "не менее"
        if (self.meeting_quorum_numerator, self.meeting_quorum_denominator) == (1, 2):
            return f"{prefix} половины состава"
        return f"{prefix} {self.meeting_quorum_numerator}/{self.meeting_quorum_denominator} состава"

    @property
    def vote_quorum_label(self):
        return f"не менее {self.vote_quorum_numerator}/{self.vote_quorum_denominator} членов комиссии"


class Rubric(models.Model):
    """Рубрикатор вопросов комиссии (для содержательного отчёта)."""

    commission = models.ForeignKey(Commission, on_delete=models.CASCADE, related_name="rubrics")
    name = models.CharField("Рубрика", max_length=200)
    sort = models.PositiveSmallIntegerField("Порядок", default=0)

    class Meta:
        verbose_name = "Рубрика вопросов"
        verbose_name_plural = "Рубрикатор вопросов"
        ordering = ["commission", "sort", "name"]
        unique_together = [("commission", "name")]

    def __str__(self):
        return self.name


class Order(models.Model):
    """Приказ по комиссии (о составе и др.)."""

    class Status(models.TextChoices):
        DRAFT = "draft", "Проект"
        SIGNED = "signed", "Подписан"
        CANCELLED = "cancelled", "Отменён"

    commission = models.ForeignKey(Commission, on_delete=models.CASCADE, related_name="orders")
    title = models.CharField("Заголовок", max_length=300, default="О составе комиссии")
    preamble = models.TextField("Преамбула", blank=True)
    number = models.CharField("Номер", max_length=50, blank=True)
    signed_on = models.DateField("Дата подписания", null=True, blank=True)
    status = models.CharField("Статус", max_length=20, choices=Status.choices, default=Status.DRAFT)
    draft_file = models.FileField("Проект (DOCX)", upload_to=upload_path, blank=True)
    scan_file = models.FileField("Скан подписанного приказа (PDF)", upload_to=upload_path, blank=True)
    note = models.TextField("Примечание", blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Приказ"
        verbose_name_plural = "Приказы"
        ordering = ["-signed_on", "-created_at"]

    def __str__(self):
        if self.number:
            return f"Приказ № {self.number} от {self.signed_on:%d.%m.%Y}" if self.signed_on else f"Приказ № {self.number}"
        return f"Проект приказа «{self.title}»"


class MemberRecord(models.Model):
    """Период участия пользователя в комиссии в определённой роли.

    Период начинается и заканчивается датой подписания приказа.
    """

    class Role(models.TextChoices):
        CHAIRMAN = "chairman", "Председатель"
        DEPUTY = "deputy", "Заместитель председателя"
        SECRETARY = "secretary", "Секретарь"
        MEMBER = "member", "Член комиссии"

    ROLE_ORDER = {Role.CHAIRMAN: 0, Role.DEPUTY: 1, Role.MEMBER: 2, Role.SECRETARY: 3}

    commission = models.ForeignKey(Commission, on_delete=models.CASCADE, related_name="member_records")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="member_records")
    role = models.CharField("Роль", max_length=20, choices=Role.choices)
    role_order = models.PositiveSmallIntegerField(default=2, editable=False)
    position_text = models.CharField("Должность (на момент включения)", max_length=200, blank=True)
    city_text = models.CharField("Город (на момент включения)", max_length=100, blank=True)
    start_date = models.DateField("Входит в состав с")
    end_date = models.DateField("Выведен из состава с", null=True, blank=True)
    start_order = models.ForeignKey(Order, null=True, blank=True, on_delete=models.PROTECT, related_name="started_records")
    end_order = models.ForeignKey(Order, null=True, blank=True, on_delete=models.PROTECT, related_name="ended_records")

    class Meta:
        verbose_name = "Период членства"
        verbose_name_plural = "История состава"
        ordering = ["commission", "role_order", "user__last_name", "start_date"]

    def __str__(self):
        return f"{self.user} — {self.get_role_display()} ({self.commission})"

    def save(self, *args, **kwargs):
        self.role_order = self.ROLE_ORDER.get(self.role, 2)
        if not self.position_text and self.user.position_id:
            self.position_text = self.user.position.name
        if not self.city_text and self.user.city_id:
            self.city_text = self.user.city.name
        super().save(*args, **kwargs)

    @property
    def is_current(self):
        today = timezone.localdate()
        return self.start_date <= today and (self.end_date is None or self.end_date > today)


class CompositionChange(models.Model):
    """Проект изменения состава. Вступает в силу с даты подписания приказа."""

    class Action(models.TextChoices):
        ADD = "add", "Ввести в состав"
        REMOVE = "remove", "Вывести из состава"
        CHANGE_ROLE = "change_role", "Изменить роль"
        UPDATE_POSITION = "update_position", "Изменить должность"

    class Status(models.TextChoices):
        PROJECT = "project", "Проект состава"
        APPLIED = "applied", "Действует"
        CANCELLED = "cancelled", "Отменено"

    commission = models.ForeignKey(Commission, on_delete=models.CASCADE, related_name="composition_changes")
    action = models.CharField("Изменение", max_length=20, choices=Action.choices)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, verbose_name="Сотрудник", on_delete=models.PROTECT, related_name="+")
    new_role = models.CharField("Роль", max_length=20, choices=MemberRecord.Role.choices, blank=True)
    new_position = models.CharField("Новая должность", max_length=200, blank=True)
    basis = models.CharField("Основание", max_length=300, blank=True)
    order = models.ForeignKey(Order, verbose_name="Приказ", null=True, blank=True, on_delete=models.SET_NULL, related_name="changes")
    status = models.CharField("Статус", max_length=20, choices=Status.choices, default=Status.PROJECT)
    applied_on = models.DateField("Вступило в силу", null=True, blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Изменение состава"
        verbose_name_plural = "Изменения состава"
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.get_action_display()}: {self.user}"


class Regulation(models.Model):
    """Редакция положения о комиссии."""

    commission = models.ForeignKey(Commission, on_delete=models.CASCADE, related_name="regulations")
    version = models.PositiveSmallIntegerField("Редакция", default=1)
    title = models.CharField("Наименование", max_length=300, default="Положение о комиссии")
    approved_on = models.DateField("Дата утверждения")
    order_number = models.CharField("Номер утверждающего приказа", max_length=50, blank=True)
    order_date = models.DateField("Дата утверждающего приказа", null=True, blank=True)
    file = models.FileField("Файл (PDF/DOCX)", upload_to=upload_path)
    review_on = models.DateField("Дата планового пересмотра", null=True, blank=True)
    expired_on = models.DateField("Утратила силу с", null=True, blank=True)
    review_reminder_sent = models.BooleanField(default=False, editable=False)
    uploaded_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Положение о комиссии"
        verbose_name_plural = "Положения о комиссиях"
        ordering = ["commission", "-version"]
        unique_together = [("commission", "version")]

    def __str__(self):
        return f"{self.title} ({self.commission}), ред. {self.version}"

    @property
    def is_current(self):
        return self.expired_on is None


class LegalAct(models.Model):
    """Нормативный правовой акт, связанный с комиссией."""

    class Status(models.TextChoices):
        ACTUAL = "actual", "Актуален"
        LOST_FORCE = "lost_force", "Утратил силу"
        UNAVAILABLE = "unavailable", "Недоступен"
        NEEDS_CHECK = "needs_check", "Требует проверки"
        RETIRED = "retired", "Утратил силу, замена не требуется"

    commission = models.ForeignKey(Commission, on_delete=models.CASCADE, related_name="legal_acts")
    title = models.CharField("Наименование", max_length=500)
    number = models.CharField("Номер", max_length=100, blank=True)
    act_date = models.DateField("Дата", null=True, blank=True)
    authority = models.CharField("Орган", max_length=200, blank=True, default="МЗ РК")
    url = models.URLField("Ссылка на adilet.zan.kz", max_length=500, blank=True)
    note = models.CharField("Примечание", max_length=500, blank=True)
    status = models.CharField("Статус", max_length=20, choices=Status.choices, default=Status.NEEDS_CHECK)
    last_checked_at = models.DateTimeField("Последняя проверка", null=True, blank=True)
    replaced_by = models.ForeignKey(
        "self", verbose_name="Заменён на", null=True, blank=True, on_delete=models.SET_NULL, related_name="replaces"
    )
    sort = models.PositiveSmallIntegerField("Порядок", default=0)

    class Meta:
        verbose_name = "НПА"
        verbose_name_plural = "Нормативная база"
        ordering = ["commission", "sort", "id"]

    def __str__(self):
        return f"{self.title} {self.number}".strip()

    @property
    def needs_attention(self):
        return self.status in {self.Status.LOST_FORCE, self.Status.UNAVAILABLE, self.Status.NEEDS_CHECK}


class LegalActCheck(models.Model):
    """Журнал проверок актуальности НПА."""

    act = models.ForeignKey(LegalAct, on_delete=models.CASCADE, related_name="checks")
    checked_at = models.DateTimeField("Дата проверки", auto_now_add=True)
    url = models.URLField(max_length=500, blank=True)
    result = models.CharField("Результат", max_length=20, choices=LegalAct.Status.choices)
    details = models.TextField("Подробности", blank=True)
    manual = models.BooleanField("Внеплановая", default=False)
    initiated_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")

    class Meta:
        verbose_name = "Проверка НПА"
        verbose_name_plural = "Журнал проверок НПА"
        ordering = ["-checked_at"]


class CommissionAccess(models.Model):
    """Доступ руководителя-наблюдателя к комиссии (назначает администратор)."""

    commission = models.ForeignKey(Commission, on_delete=models.CASCADE, related_name="observers")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="observed_commissions")
    granted_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    granted_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Доступ наблюдателя"
        verbose_name_plural = "Доступы наблюдателей"
        unique_together = [("commission", "user")]

    def __str__(self):
        return f"{self.user} → {self.commission}"


class DocumentTemplate(models.Model):
    """Шаблон документа DOCX с плейсхолдерами (docxtpl / Jinja2)."""

    class Kind(models.TextChoices):
        ORDER = "order", "Приказ о составе"
        AGENDA = "agenda", "Повестка"
        PROTOCOL = "protocol", "Протокол заседания"
        ABSENTEE_PROTOCOL = "absentee_protocol", "Протокол заочного голосования"
        REPORT = "report", "Отчёт"

    kind = models.CharField("Вид", max_length=30, choices=Kind.choices)
    commission = models.ForeignKey(
        Commission, verbose_name="Комиссия", null=True, blank=True, on_delete=models.CASCADE,
        help_text="Пусто — шаблон по умолчанию для всех комиссий.",
    )
    file = models.FileField("Файл DOCX", upload_to=upload_path)
    is_active = models.BooleanField("Используется", default=True)
    uploaded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Шаблон документа"
        verbose_name_plural = "Шаблоны документов"

    def __str__(self):
        return f"{self.get_kind_display()} — {self.commission or 'все комиссии'}"

    @classmethod
    def find(cls, kind, commission=None):
        qs = cls.objects.filter(kind=kind, is_active=True)
        return (
            qs.filter(commission=commission).order_by("-uploaded_at").first()
            or qs.filter(commission__isnull=True).order_by("-uploaded_at").first()
        )


class AbsenceReason(models.Model):
    name = models.CharField("Причина", max_length=200, unique=True)
    is_excused = models.BooleanField("Уважительная", default=True)

    class Meta:
        verbose_name = "Причина отсутствия"
        verbose_name_plural = "Причины отсутствия"
        ordering = ["name"]

    def __str__(self):
        return self.name
