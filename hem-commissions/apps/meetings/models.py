import re

from django.conf import settings
from django.db import models
from django.utils import timezone

from apps.accounts.models import City
from apps.committees.models import AbsenceReason, Commission, Rubric, upload_path

PATIENT_ID_RE = re.compile(r"^[A-Za-zА-Яа-я0-9\-/_.]{1,40}$")


class Meeting(models.Model):
    class Kind(models.TextChoices):
        PLANNED = "planned", "Плановое"
        EXTRA = "extra", "Внеочередное"

    class Format(models.TextChoices):
        OFFLINE = "offline", "Очное"
        ONLINE = "online", "Онлайн"
        MIXED = "mixed", "Смешанное"

    class Status(models.TextChoices):
        PLANNED = "planned", "Запланировано"
        HELD = "held", "Проведено"
        POSTPONED = "postponed", "Перенесено"
        CANCELLED = "cancelled", "Отменено"

    class AgendaStatus(models.TextChoices):
        DRAFT = "draft", "Формируется"
        SUBMITTED = "submitted", "На утверждении у председателя"
        APPROVED = "approved", "Утверждена председателем"

    commission = models.ForeignKey(Commission, on_delete=models.CASCADE, related_name="meetings")
    kind = models.CharField("Тип", max_length=20, choices=Kind.choices, default=Kind.PLANNED)
    starts_at = models.DateTimeField("Дата и время")
    duration_minutes = models.PositiveSmallIntegerField("Длительность, мин", default=90)
    format = models.CharField("Формат", max_length=20, choices=Format.choices, default=Format.OFFLINE)
    place = models.CharField("Место", max_length=300, blank=True)
    video_link = models.URLField("Ссылка на видеоконференцию", blank=True)
    cities = models.ManyToManyField(City, verbose_name="Площадки-участники", blank=True)
    status = models.CharField("Статус", max_length=20, choices=Status.choices, default=Status.PLANNED)
    original_starts_at = models.DateTimeField("Исходная дата", null=True, blank=True, editable=False)
    cancel_reason = models.CharField("Причина отмены", max_length=300, blank=True)
    agenda_status = models.CharField("Повестка", max_length=20, choices=AgendaStatus.choices, default=AgendaStatus.DRAFT)
    agenda_approved_at = models.DateTimeField(null=True, blank=True)
    agenda_approved_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    proposals_deadline = models.DateTimeField(
        "Приём предложений в повестку до", null=True, blank=True,
    )
    reminder_3d_sent = models.BooleanField(default=False, editable=False)
    reminder_1d_sent = models.BooleanField(default=False, editable=False)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Заседание"
        verbose_name_plural = "Заседания"
        ordering = ["starts_at"]

    def __str__(self):
        local = timezone.localtime(self.starts_at)
        return f"{self.commission.short_name}, заседание {local:%d.%m.%Y %H:%M}"

    @property
    def meeting_date(self):
        return timezone.localtime(self.starts_at).date()

    @property
    def is_open_for_proposals(self):
        if self.status not in (self.Status.PLANNED, self.Status.POSTPONED) or self.agenda_status == self.AgendaStatus.APPROVED:
            return False
        return self.proposals_deadline is None or self.proposals_deadline > timezone.now()

    @property
    def is_upcoming(self):
        return self.status in (self.Status.PLANNED, self.Status.POSTPONED)

    @property
    def ends_at(self):
        from datetime import timedelta

        return self.starts_at + timedelta(minutes=self.duration_minutes or 0)

    def composition(self):
        """Состав комиссии на дату заседания."""
        return self.commission.members_on(self.meeting_date)

    def quorum(self):
        """(присутствует, всего, требуется, достигнут)."""
        members = list(self.composition().values_list("user_id", flat=True))
        total = len(set(members))
        present = self.attendance.filter(
            user_id__in=members, status__in=[Attendance.Status.PRESENT, Attendance.Status.ONLINE]
        ).count()
        needed = self.commission.meeting_quorum_needed(total) if total else 0
        return {"present": present, "total": total, "needed": needed, "reached": total > 0 and present >= needed}


class MeetingReschedule(models.Model):
    meeting = models.ForeignKey(Meeting, on_delete=models.CASCADE, related_name="reschedules")
    old_starts_at = models.DateTimeField("Было")
    new_starts_at = models.DateTimeField("Стало")
    reason = models.CharField("Причина", max_length=300, blank=True)
    changed_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+")
    changed_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Перенос заседания"
        verbose_name_plural = "Переносы заседаний"
        ordering = ["-changed_at"]


class AgendaItem(models.Model):
    meeting = models.ForeignKey(Meeting, on_delete=models.CASCADE, related_name="agenda_items")
    position = models.PositiveSmallIntegerField("№", default=1)
    title = models.CharField("Вопрос", max_length=500)
    description = models.TextField("Пояснение", blank=True)
    speaker = models.ForeignKey(
        settings.AUTH_USER_MODEL, verbose_name="Докладчик", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    speaker_name = models.CharField("Докладчик (не пользователь системы)", max_length=200, blank=True)
    duration_minutes = models.PositiveSmallIntegerField("Регламент, мин", default=10)
    rubric = models.ForeignKey(Rubric, verbose_name="Рубрика", null=True, blank=True, on_delete=models.SET_NULL)
    patient_id = models.CharField(
        "ID пациента (МИС)", max_length=40, blank=True, db_index=True,
        help_text="Только внутренний идентификатор пациента из МИС — без ФИО, ИИН и даты рождения.",
    )
    requires_vote = models.BooleanField("Выносится на голосование", default=False)
    is_control_item = models.BooleanField("Контроль исполнения решений", default=False, editable=False)
    proposal = models.ForeignKey("AgendaProposal", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")

    class Meta:
        verbose_name = "Вопрос повестки"
        verbose_name_plural = "Повестка"
        ordering = ["meeting", "position", "id"]

    def __str__(self):
        return f"{self.position}. {self.title}"

    @property
    def speaker_display(self):
        return self.speaker.short_name if self.speaker else self.speaker_name


class AgendaProposal(models.Model):
    """Предложение члена комиссии о внесении вопроса в повестку."""

    class Status(models.TextChoices):
        NEW = "new", "На рассмотрении"
        ACCEPTED = "accepted", "Включён в повестку"
        REJECTED = "rejected", "Отклонён"

    meeting = models.ForeignKey(Meeting, on_delete=models.CASCADE, related_name="proposals")
    author = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="agenda_proposals")
    title = models.CharField("Вопрос", max_length=500)
    description = models.TextField("Обоснование", blank=True)
    speaker_name = models.CharField("Предлагаемый докладчик", max_length=200, blank=True)
    patient_id = models.CharField("ID пациента (МИС)", max_length=40, blank=True)
    requires_vote = models.BooleanField("Требуется голосование", default=False)
    file = models.FileField("Материал", upload_to=upload_path, blank=True)
    file_name = models.CharField(max_length=255, blank=True)
    status = models.CharField("Статус", max_length=20, choices=Status.choices, default=Status.NEW)
    response = models.CharField("Ответ секретаря", max_length=500, blank=True)
    reviewed_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Предложение в повестку"
        verbose_name_plural = "Предложения в повестку"
        ordering = ["-created_at"]

    def __str__(self):
        return self.title


class AgendaAcknowledgement(models.Model):
    """Согласование повестки членом комиссии: «ознакомлен» или предложение."""

    class Status(models.TextChoices):
        ACK = "ack", "Ознакомлен"
        SUGGESTION = "suggestion", "Предложение по повестке"

    meeting = models.ForeignKey(Meeting, on_delete=models.CASCADE, related_name="acknowledgements")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="+")
    status = models.CharField(max_length=20, choices=Status.choices)
    text = models.TextField("Предложение", blank=True)
    created_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Ознакомление с повесткой"
        verbose_name_plural = "Ознакомление с повесткой"
        unique_together = [("meeting", "user")]


class Material(models.Model):
    class Kind(models.TextChoices):
        FILE = "file", "Файл"
        LINK = "link", "Ссылка"
        AUDIO = "audio", "Аудиозапись (ссылка)"
        VIDEO = "video", "Видеозапись (ссылка)"

    meeting = models.ForeignKey(Meeting, on_delete=models.CASCADE, related_name="materials")
    agenda_item = models.ForeignKey(AgendaItem, verbose_name="Вопрос повестки", null=True, blank=True, on_delete=models.SET_NULL, related_name="materials")
    kind = models.CharField("Вид", max_length=10, choices=Kind.choices, default=Kind.FILE)
    title = models.CharField("Название", max_length=300)
    file = models.FileField("Файл", upload_to=upload_path, blank=True)
    file_name = models.CharField("Исходное имя файла", max_length=255, blank=True)
    file_size = models.PositiveBigIntegerField(default=0)
    url = models.URLField("Ссылка", max_length=1000, blank=True)
    uploaded_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Материал заседания"
        verbose_name_plural = "Материалы заседаний"
        ordering = ["agenda_item__position", "created_at"]

    def __str__(self):
        return self.title


class Attendance(models.Model):
    class Status(models.TextChoices):
        PRESENT = "present", "Присутствовал очно"
        ONLINE = "online", "Присутствовал онлайн"
        ABSENT_EXCUSED = "absent_excused", "Отсутствовал по уважительной причине"
        ABSENT = "absent", "Отсутствовал"

    meeting = models.ForeignKey(Meeting, on_delete=models.CASCADE, related_name="attendance")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="attendance")
    status = models.CharField("Отметка", max_length=20, choices=Status.choices)
    reason = models.ForeignKey(AbsenceReason, verbose_name="Причина", null=True, blank=True, on_delete=models.SET_NULL)
    is_invited = models.BooleanField("Приглашённый", default=False)

    class Meta:
        verbose_name = "Отметка присутствия"
        verbose_name_plural = "Список присутствующих"
        unique_together = [("meeting", "user")]


class Invitation(models.Model):
    """Приглашённый участник: доступ только к конкретному заседанию до указанной даты."""

    meeting = models.ForeignKey(Meeting, on_delete=models.CASCADE, related_name="invitations")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="invitations")
    valid_until = models.DateField("Доступ до")
    note = models.CharField("Примечание", max_length=300, blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Приглашение"
        verbose_name_plural = "Приглашённые участники"
        unique_together = [("meeting", "user")]

    @property
    def is_valid(self):
        return self.valid_until >= timezone.localdate()


class Transcript(models.Model):
    """Транскрипт аудиозаписи заседания (аудио хранится по ссылке)."""

    class Status(models.TextChoices):
        PENDING = "pending", "В очереди"
        PROCESSING = "processing", "Распознаётся"
        DONE = "done", "Готов"
        FAILED = "failed", "Ошибка"
        MANUAL = "manual", "Внесён вручную"

    meeting = models.ForeignKey(Meeting, on_delete=models.CASCADE, related_name="transcripts")
    audio = models.ForeignKey(Material, verbose_name="Аудиозапись", null=True, blank=True, on_delete=models.SET_NULL)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)
    text = models.TextField("Текст с таймкодами", blank=True)
    error = models.TextField(blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Транскрипт"
        verbose_name_plural = "Транскрипты"
        ordering = ["-created_at"]
