import uuid

from django.conf import settings
from django.db import models
from django.utils import timezone

from apps.committees.models import Commission, upload_path
from apps.meetings.models import AgendaItem, Meeting


class VotingSession(models.Model):
    """Электронное голосование по вопросу.

    Используется и для вопросов повестки заседания, и для заочного голосования.
    Члены комиссии получают письмо со ссылкой и голосуют в личном кабинете.
    Кворум — не менее 2/3 членов комиссии (настраивается в комиссии);
    решение принято, если «за» более половины проголосовавших.
    """

    class Status(models.TextChoices):
        DRAFT = "draft", "Подготовка"
        OPEN = "open", "Идёт голосование"
        CLOSED = "closed", "Завершено"
        CANCELLED = "cancelled", "Отменено"

    class Outcome(models.TextChoices):
        ADOPTED = "adopted", "Решение принято"
        REJECTED = "rejected", "Решение не принято"
        NO_QUORUM = "no_quorum", "Кворум не набран"

    uid = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    commission = models.ForeignKey(Commission, on_delete=models.CASCADE, related_name="votings")
    meeting = models.ForeignKey(Meeting, null=True, blank=True, on_delete=models.CASCADE, related_name="votings")
    agenda_item = models.ForeignKey(AgendaItem, null=True, blank=True, on_delete=models.CASCADE, related_name="votings")
    question = models.CharField("Вопрос", max_length=500)
    description = models.TextField("Пояснение / проект решения", blank=True)
    deadline = models.DateTimeField("Окончание голосования")
    allow_comments = models.BooleanField("Разрешить комментарий", default=True)
    status = models.CharField("Статус", max_length=20, choices=Status.choices, default=Status.DRAFT)
    eligible = models.ManyToManyField(settings.AUTH_USER_MODEL, related_name="eligible_votings", blank=True, editable=False)
    opened_at = models.DateTimeField(null=True, blank=True)
    closed_at = models.DateTimeField(null=True, blank=True)
    outcome = models.CharField("Итог", max_length=20, choices=Outcome.choices, blank=True)
    votes_for = models.PositiveSmallIntegerField(default=0)
    votes_against = models.PositiveSmallIntegerField(default=0)
    votes_abstain = models.PositiveSmallIntegerField(default=0)
    quorum_needed = models.PositiveSmallIntegerField(default=0)
    reminder_sent = models.BooleanField(default=False, editable=False)
    initiated_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Голосование"
        verbose_name_plural = "Голосования"
        ordering = ["-created_at"]

    def __str__(self):
        return self.question

    @property
    def is_absentee(self):
        return self.meeting_id is None

    @property
    def is_open(self):
        return self.status == self.Status.OPEN and self.deadline > timezone.now()

    @property
    def open_results(self):
        return self.commission.open_voting


class VotingMaterial(models.Model):
    session = models.ForeignKey(VotingSession, on_delete=models.CASCADE, related_name="materials")
    title = models.CharField("Название", max_length=300)
    file = models.FileField(upload_to=upload_path, blank=True)
    file_name = models.CharField(max_length=255, blank=True)
    url = models.URLField(max_length=1000, blank=True)

    class Meta:
        verbose_name = "Материал голосования"
        verbose_name_plural = "Материалы голосования"


class Vote(models.Model):
    class Choice(models.TextChoices):
        FOR = "for", "За"
        AGAINST = "against", "Против"
        ABSTAIN = "abstain", "Воздержался"

    session = models.ForeignKey(VotingSession, on_delete=models.CASCADE, related_name="votes")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="votes")
    choice = models.CharField("Голос", max_length=10, choices=Choice.choices)
    comment = models.TextField("Комментарий", blank=True)
    cast_at = models.DateTimeField("Время голоса", auto_now=True)
    entered_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+",
        help_text="Заполнено, если голос внесён секретарём со слов члена комиссии.",
    )

    class Meta:
        verbose_name = "Голос"
        verbose_name_plural = "Голоса"
        unique_together = [("session", "user")]
