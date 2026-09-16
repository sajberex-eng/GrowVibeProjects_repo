from django.conf import settings
from django.db import models


class AuditLogQuerySet(models.QuerySet):
    def update(self, **kwargs):
        raise PermissionError("Журнал аудита не редактируется")

    def delete(self):
        raise PermissionError("Журнал аудита не редактируется")


class AuditLog(models.Model):
    """Неизменяемый журнал действий пользователей."""

    created_at = models.DateTimeField("Дата и время", auto_now_add=True, db_index=True)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, verbose_name="Пользователь", null=True, blank=True,
        on_delete=models.SET_NULL, related_name="+",
    )
    ip_address = models.GenericIPAddressField("IP-адрес", null=True, blank=True)
    action = models.CharField("Действие", max_length=50, db_index=True)
    object_type = models.CharField("Тип объекта", max_length=100, blank=True, db_index=True)
    object_id = models.CharField("ID объекта", max_length=64, blank=True)
    object_repr = models.CharField("Объект", max_length=300, blank=True)
    details = models.JSONField("Подробности", default=dict, blank=True)

    objects = AuditLogQuerySet.as_manager()

    class Meta:
        verbose_name = "Запись журнала аудита"
        verbose_name_plural = "Журнал аудита"
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.created_at:%d.%m.%Y %H:%M} {self.action} {self.object_repr}"

    def save(self, *args, **kwargs):
        if self.pk:
            raise PermissionError("Журнал аудита не редактируется")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise PermissionError("Журнал аудита не редактируется")
