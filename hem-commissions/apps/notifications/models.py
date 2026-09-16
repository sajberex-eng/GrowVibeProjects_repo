from django.conf import settings
from django.db import models


class Event(models.TextChoices):
    MEETING_SCHEDULED = "meeting_scheduled", "Назначено / перенесено / отменено заседание"
    MEETING_REMINDER = "meeting_reminder", "Напоминание о заседании"
    AGENDA_APPROVAL = "agenda_approval", "Повестка на ознакомление"
    AGENDA_PROPOSAL = "agenda_proposal", "Предложения в повестку"
    MATERIALS_ADDED = "materials_added", "Добавлены материалы"
    PROTOCOL_APPROVAL = "protocol_approval", "Проект протокола на согласование"
    PROTOCOL_SIGNING = "protocol_signing", "Протокол на подпись"
    PROTOCOL_SIGNED = "protocol_signed", "Протокол подписан"
    VOTING = "voting", "Голосование: открыто / напоминание / итоги"
    ASSIGNMENT = "assignment", "Поручения: назначение / напоминание / просрочка"
    LEGAL_ACT = "legal_act", "НПА утратил силу или недоступен"
    REGULATION_REVIEW = "regulation_review", "Плановый пересмотр положения"
    COMPOSITION = "composition", "Изменения состава и приказы"
    SYSTEM = "system", "Системные сообщения"


# Критические события: отключить уведомление нельзя ни в одном обязательном канале.
CRITICAL_EVENTS = {Event.PROTOCOL_SIGNING, Event.VOTING, Event.ASSIGNMENT}


class Notification(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="notifications")
    event = models.CharField("Событие", max_length=40, choices=Event.choices)
    title = models.CharField("Заголовок", max_length=300)
    body = models.TextField("Текст", blank=True)
    url = models.CharField("Ссылка", max_length=500, blank=True)
    commission = models.ForeignKey("committees.Commission", null=True, blank=True, on_delete=models.CASCADE, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    read_at = models.DateTimeField(null=True, blank=True)
    emailed_at = models.DateTimeField(null=True, blank=True)
    whatsapp_sent_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = "Уведомление"
        verbose_name_plural = "Уведомления"
        ordering = ["-created_at"]

    def __str__(self):
        return self.title


class JobRun(models.Model):
    """Отметка о последнем запуске периодической задачи (для планировщика)."""

    name = models.CharField("Задача", max_length=100, unique=True)
    last_run = models.DateTimeField("Последний запуск", null=True, blank=True)
    last_result = models.JSONField("Результат", default=dict, blank=True)

    class Meta:
        verbose_name = "Запуск периодической задачи"
        verbose_name_plural = "Запуски периодических задач"

    def __str__(self):
        return self.name


class NotificationPreference(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="notification_preferences")
    event = models.CharField(max_length=40, choices=Event.choices)
    email = models.BooleanField("E-mail", default=True)
    whatsapp = models.BooleanField("WhatsApp", default=False)

    class Meta:
        verbose_name = "Настройка уведомлений"
        verbose_name_plural = "Настройки уведомлений"
        unique_together = [("user", "event")]
