import uuid

from django.conf import settings
from django.db import models
from django.utils import timezone

from apps.committees.models import Commission, MemberRecord, upload_path
from apps.meetings.models import AgendaItem, Meeting


class Protocol(models.Model):
    """Протокол заседания или заочного голосования.

    Жизненный цикл: черновик → согласование членами (в этот период принимаются
    особые мнения) → подпись (секретарь, затем председатель) → подписан.
    При переходе на подпись документ «замораживается»: формируется PDF и его
    хэш SHA-256; подписанты подписывают именно этот хэш. После второй подписи
    формируется итоговый PDF со штампом подписей и QR-кодом.
    """

    class Kind(models.TextChoices):
        MEETING = "meeting", "Протокол заседания"
        ABSENTEE = "absentee", "Протокол заочного голосования"

    class Status(models.TextChoices):
        DRAFT = "draft", "Черновик"
        APPROVAL = "approval", "На согласовании"
        SIGNING = "signing", "На подписи"
        SIGNED = "signed", "Подписан"
        ANNULLED = "annulled", "Аннулирован"

    uid = models.UUIDField("Идентификатор для проверки", default=uuid.uuid4, unique=True, editable=False)
    commission = models.ForeignKey(Commission, on_delete=models.PROTECT, related_name="protocols")
    kind = models.CharField("Вид", max_length=20, choices=Kind.choices, default=Kind.MEETING)
    meeting = models.ForeignKey(Meeting, null=True, blank=True, on_delete=models.PROTECT, related_name="protocols")
    votings = models.ManyToManyField("voting.VotingSession", blank=True, related_name="protocols")
    number = models.CharField("Номер", max_length=50, blank=True)
    protocol_date = models.DateField("Дата")
    place = models.CharField("Место", max_length=300, blank=True)
    format_text = models.CharField("Форма проведения", max_length=100, blank=True)
    preamble = models.TextField("Вступительная часть", blank=True)
    status = models.CharField("Статус", max_length=20, choices=Status.choices, default=Status.DRAFT)
    revision = models.PositiveSmallIntegerField("Редакция", default=1)
    approval_started_at = models.DateTimeField(null=True, blank=True)
    approval_deadline = models.DateField("Срок согласования", null=True, blank=True)
    approval_reminder_sent = models.BooleanField(default=False, editable=False)
    transcription_used = models.BooleanField("Сформирован с использованием автоматической транскрипции", default=False)
    patient_ids_confirmed = models.BooleanField("Обезличивание подтверждено секретарём", default=False)
    snapshot = models.JSONField("Снимок данных на момент подписи", default=dict, blank=True, editable=False)
    frozen_pdf = models.FileField("PDF на подпись", upload_to=upload_path, blank=True, editable=False)
    frozen_hash = models.CharField("SHA-256 документа на подпись", max_length=64, blank=True, editable=False)
    signed_pdf = models.FileField("Подписанный PDF", upload_to=upload_path, blank=True, editable=False)
    signed_hash = models.CharField("SHA-256 подписанного PDF", max_length=64, blank=True, editable=False)
    signed_at = models.DateTimeField(null=True, blank=True)
    annulled_reason = models.TextField("Причина аннулирования", blank=True)
    annulled_at = models.DateTimeField(null=True, blank=True)
    replaces = models.ForeignKey(
        "self", verbose_name="Взамен аннулированного", null=True, blank=True, on_delete=models.PROTECT, related_name="replaced_by"
    )
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Протокол"
        verbose_name_plural = "Протоколы"
        ordering = ["-protocol_date", "-id"]

    def __str__(self):
        return f"Протокол {self.commission.short_name} № {self.number or 'б/н'} от {self.protocol_date:%d.%m.%Y}"

    @property
    def is_editable(self):
        return self.status == self.Status.DRAFT

    @property
    def is_locked(self):
        return self.status in {self.Status.SIGNING, self.Status.SIGNED, self.Status.ANNULLED}

    @property
    def verify_url(self):
        return f"{settings.SITE_URL}/verify/{self.uid}/"

    def active_signatures(self):
        return self.signatures.filter(revoked_at__isnull=True).order_by("signed_at")

    def next_signer_role(self):
        roles = set(self.active_signatures().values_list("role", flat=True))
        if MemberRecord.Role.SECRETARY not in roles:
            return MemberRecord.Role.SECRETARY
        if MemberRecord.Role.CHAIRMAN not in roles:
            return MemberRecord.Role.CHAIRMAN
        return None


class ProtocolItem(models.Model):
    """Раздел протокола по вопросу повестки: слушали / выступили / решили."""

    protocol = models.ForeignKey(Protocol, on_delete=models.CASCADE, related_name="items")
    agenda_item = models.ForeignKey(AgendaItem, null=True, blank=True, on_delete=models.SET_NULL, related_name="protocol_items")
    voting = models.ForeignKey("voting.VotingSession", null=True, blank=True, on_delete=models.SET_NULL, related_name="protocol_items")
    position = models.PositiveSmallIntegerField("№", default=1)
    title = models.CharField("Вопрос", max_length=500)
    patient_id = models.CharField("ID пациента (МИС)", max_length=40, blank=True, db_index=True)
    heard = models.TextField("Слушали", blank=True)
    discussed = models.TextField("Выступили", blank=True)
    resolved = models.TextField("Решили", blank=True)
    vote_summary = models.CharField("Результаты голосования", max_length=300, blank=True)
    flagged_names = models.JSONField("Возможные ФИО пациентов (требуют замены на ID)", default=list, blank=True)

    class Meta:
        verbose_name = "Вопрос протокола"
        verbose_name_plural = "Вопросы протокола"
        ordering = ["protocol", "position", "id"]

    def __str__(self):
        return f"{self.position}. {self.title}"


class ProtocolApproval(models.Model):
    """Согласование проекта протокола членом комиссии (по редакции)."""

    class Status(models.TextChoices):
        PENDING = "pending", "Нет ответа"
        AGREED = "agreed", "Согласен"
        REMARKS = "remarks", "Замечания"

    protocol = models.ForeignKey(Protocol, on_delete=models.CASCADE, related_name="approvals")
    revision = models.PositiveSmallIntegerField(default=1)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="protocol_approvals")
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)
    item = models.ForeignKey(ProtocolItem, verbose_name="К вопросу", null=True, blank=True, on_delete=models.SET_NULL)
    remarks = models.TextField("Замечания", blank=True)
    responded_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = "Согласование протокола"
        verbose_name_plural = "Согласования протоколов"
        unique_together = [("protocol", "revision", "user")]


class DissentingOpinion(models.Model):
    """Особое мнение. Подаётся только в период согласования проекта протокола."""

    protocol = models.ForeignKey(Protocol, on_delete=models.CASCADE, related_name="dissents")
    item = models.ForeignKey(ProtocolItem, verbose_name="По вопросу", on_delete=models.CASCADE, related_name="dissents")
    author = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="dissents")
    text = models.TextField("Текст особого мнения")
    file = models.FileField("Файл", upload_to=upload_path, blank=True)
    file_name = models.CharField(max_length=255, blank=True)
    signed_at = models.DateTimeField("Подписано", null=True, blank=True)
    signed_ip = models.GenericIPAddressField(null=True, blank=True)
    text_hash = models.CharField(max_length=64, blank=True)
    withdrawn_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Особое мнение"
        verbose_name_plural = "Особые мнения"
        ordering = ["item__position", "created_at"]


class Signature(models.Model):
    """Простая электронная подпись протокола (подтверждение кодом из e-mail)."""

    protocol = models.ForeignKey(Protocol, on_delete=models.CASCADE, related_name="signatures")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="signatures")
    role = models.CharField(max_length=20, choices=MemberRecord.Role.choices)
    full_name = models.CharField("ФИО подписанта", max_length=300)
    position_text = models.CharField("Должность", max_length=200, blank=True)
    signed_at = models.DateTimeField("Дата и время подписи", default=timezone.now)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    document_hash = models.CharField("Подписанный хэш SHA-256", max_length=64)
    method = models.CharField("Способ", max_length=50, default="simple_otp_email")
    revoked_at = models.DateTimeField(null=True, blank=True)
    revoke_reason = models.CharField(max_length=300, blank=True)

    class Meta:
        verbose_name = "Подпись"
        verbose_name_plural = "Подписи"
        ordering = ["signed_at"]


class Decision(models.Model):
    protocol = models.ForeignKey(Protocol, on_delete=models.CASCADE, related_name="decisions")
    item = models.ForeignKey(ProtocolItem, on_delete=models.CASCADE, related_name="decisions")
    number = models.CharField("№ решения", max_length=20, blank=True)
    text = models.TextField("Текст решения")

    class Meta:
        verbose_name = "Решение"
        verbose_name_plural = "Решения"
        ordering = ["item__position", "id"]

    def __str__(self):
        return f"{self.number} {self.text[:80]}".strip()

    @property
    def commission(self):
        return self.protocol.commission


class Assignment(models.Model):
    class Status(models.TextChoices):
        ASSIGNED = "assigned", "Назначено"
        IN_PROGRESS = "in_progress", "В работе"
        ON_CONFIRMATION = "on_confirmation", "На подтверждении"
        DONE = "done", "Исполнено"
        DONE_LATE = "done_late", "Исполнено с нарушением срока"
        CANCELLED = "cancelled", "Снято"

    OPEN_STATUSES = (Status.ASSIGNED, Status.IN_PROGRESS, Status.ON_CONFIRMATION)

    decision = models.ForeignKey(Decision, on_delete=models.CASCADE, related_name="assignments")
    commission = models.ForeignKey(Commission, on_delete=models.PROTECT, related_name="assignments", editable=False)
    text = models.TextField("Поручение")
    responsible = models.ForeignKey(
        settings.AUTH_USER_MODEL, verbose_name="Ответственный", null=True, blank=True, on_delete=models.PROTECT, related_name="assignments"
    )
    responsible_name = models.CharField("Ответственный (вне системы)", max_length=200, blank=True)
    co_executors = models.ManyToManyField(settings.AUTH_USER_MODEL, verbose_name="Соисполнители", blank=True, related_name="co_assignments")
    due_date = models.DateField("Срок")
    status = models.CharField("Статус", max_length=20, choices=Status.choices, default=Status.ASSIGNED)
    reported_at = models.DateTimeField("Отмечено исполнение", null=True, blank=True)
    completed_on = models.DateField("Дата исполнения", null=True, blank=True)
    confirmed_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    cancel_reason = models.CharField("Причина снятия", max_length=300, blank=True)
    reminded_7d = models.BooleanField(default=False, editable=False)
    reminded_1d = models.BooleanField(default=False, editable=False)
    overdue_notified = models.BooleanField(default=False, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Поручение"
        verbose_name_plural = "Поручения"
        ordering = ["due_date", "id"]

    def __str__(self):
        return self.text[:100]

    def save(self, *args, **kwargs):
        if not self.commission_id:
            self.commission_id = self.decision.protocol.commission_id
        super().save(*args, **kwargs)

    @property
    def responsible_display(self):
        return self.responsible.short_name if self.responsible else self.responsible_name

    @property
    def is_open(self):
        return self.status in self.OPEN_STATUSES

    @property
    def is_overdue(self):
        return self.is_open and self.due_date < timezone.localdate()


class AssignmentComment(models.Model):
    assignment = models.ForeignKey(Assignment, on_delete=models.CASCADE, related_name="comments")
    author = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    text = models.TextField("Комментарий", blank=True)
    file = models.FileField("Файл-подтверждение", upload_to=upload_path, blank=True)
    file_name = models.CharField(max_length=255, blank=True)
    status_change = models.CharField(max_length=50, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Комментарий к поручению"
        verbose_name_plural = "Комментарии к поручениям"
        ordering = ["created_at"]
