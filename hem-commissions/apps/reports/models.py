import uuid

from django.conf import settings
from django.db import models

from apps.committees.models import Commission, upload_path


class SignedReport(models.Model):
    """Отчёт о работе комиссии, подписанный председателем (простая ЭП)."""

    uid = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    commission = models.ForeignKey(Commission, null=True, blank=True, on_delete=models.PROTECT, related_name="signed_reports")
    period_start = models.DateField("Период с")
    period_end = models.DateField("Период по")
    pdf = models.FileField(upload_to=upload_path)
    document_hash = models.CharField(max_length=64)
    signed_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    signed_at = models.DateTimeField(auto_now_add=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)

    class Meta:
        verbose_name = "Подписанный отчёт"
        verbose_name_plural = "Подписанные отчёты"
        ordering = ["-signed_at"]

    def __str__(self):
        name = self.commission.short_name if self.commission else "Все комиссии"
        return f"Отчёт {name} за {self.period_start:%d.%m.%Y}–{self.period_end:%d.%m.%Y}"
