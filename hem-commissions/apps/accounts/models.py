import hashlib
import secrets
from datetime import timedelta

from django.conf import settings
from django.contrib.auth.models import AbstractUser
from django.db import models
from django.utils import timezone


class City(models.Model):
    """Город (площадка) Центра."""

    name = models.CharField("Наименование", max_length=100, unique=True)

    class Meta:
        verbose_name = "Город (площадка)"
        verbose_name_plural = "Города (площадки)"
        ordering = ["name"]

    def __str__(self):
        return self.name


class Department(models.Model):
    name = models.CharField("Наименование", max_length=200, unique=True)

    class Meta:
        verbose_name = "Подразделение"
        verbose_name_plural = "Подразделения"
        ordering = ["name"]

    def __str__(self):
        return self.name


class Position(models.Model):
    name = models.CharField("Наименование", max_length=200, unique=True)

    class Meta:
        verbose_name = "Должность"
        verbose_name_plural = "Должности"
        ordering = ["name"]

    def __str__(self):
        return self.name


class User(AbstractUser):
    """Пользователь системы.

    Администратор системы — пользователь с флагом is_staff. Роли в комиссиях
    (председатель, секретарь, член) определяются действующим составом комиссии.
    """

    middle_name = models.CharField("Отчество", max_length=150, blank=True)
    position = models.ForeignKey(Position, verbose_name="Должность", null=True, blank=True, on_delete=models.SET_NULL)
    department = models.ForeignKey(Department, verbose_name="Подразделение", null=True, blank=True, on_delete=models.SET_NULL)
    city = models.ForeignKey(City, verbose_name="Город (площадка)", null=True, blank=True, on_delete=models.SET_NULL)
    phone = models.CharField("Телефон", max_length=30, blank=True)
    must_change_password = models.BooleanField("Сменить пароль при входе", default=True)
    failed_login_attempts = models.PositiveSmallIntegerField("Неудачных попыток входа", default=0)
    locked_until = models.DateTimeField("Заблокирован до", null=True, blank=True)
    dismissed_at = models.DateField("Дата увольнения", null=True, blank=True)
    whatsapp_opt_in = models.BooleanField("Получать уведомления в WhatsApp", default=False)

    class Meta:
        verbose_name = "Пользователь"
        verbose_name_plural = "Пользователи"
        ordering = ["last_name", "first_name"]

    def __str__(self):
        return self.full_name or self.username

    @property
    def full_name(self):
        return " ".join(p for p in [self.last_name, self.first_name, self.middle_name] if p)

    @property
    def short_name(self):
        """Фамилия И. О."""
        initials = "".join(f"{p[0]}." for p in [self.first_name, self.middle_name] if p)
        return f"{self.last_name} {initials}".strip() or self.username

    @property
    def is_admin(self):
        return self.is_staff or self.is_superuser

    @property
    def is_locked(self):
        return bool(self.locked_until and self.locked_until > timezone.now())

    def register_failed_login(self):
        self.failed_login_attempts += 1
        if self.failed_login_attempts >= settings.LOGIN_MAX_FAILED_ATTEMPTS:
            self.locked_until = timezone.now() + timedelta(minutes=settings.LOGIN_LOCKOUT_MINUTES)
            self.failed_login_attempts = 0
        self.save(update_fields=["failed_login_attempts", "locked_until"])

    def reset_failed_logins(self):
        if self.failed_login_attempts or self.locked_until:
            self.failed_login_attempts = 0
            self.locked_until = None
            self.save(update_fields=["failed_login_attempts", "locked_until"])

    def dismiss(self, date=None):
        """Деактивация при увольнении; история участия сохраняется."""
        self.dismissed_at = date or timezone.localdate()
        self.is_active = False
        self.save(update_fields=["dismissed_at", "is_active"])


def _hash_code(code):
    return hashlib.sha256(code.encode()).hexdigest()


class OneTimeCode(models.Model):
    """Одноразовый код подтверждения простой ЭП (отправляется на e-mail)."""

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="one_time_codes")
    purpose = models.CharField("Назначение", max_length=100)
    code_hash = models.CharField(max_length=64)
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()
    attempts = models.PositiveSmallIntegerField(default=0)
    used_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = "Одноразовый код"
        verbose_name_plural = "Одноразовые коды"

    @classmethod
    def issue(cls, user, purpose):
        """Создаёт код и возвращает его открытое значение (для отправки)."""
        cls.objects.filter(user=user, purpose=purpose, used_at__isnull=True).update(used_at=timezone.now())
        code = f"{secrets.randbelow(10**6):06d}"
        cls.objects.create(
            user=user,
            purpose=purpose,
            code_hash=_hash_code(code),
            expires_at=timezone.now() + timedelta(minutes=settings.OTP_TTL_MINUTES),
        )
        return code

    @classmethod
    def verify(cls, user, purpose, code):
        otp = (
            cls.objects.filter(user=user, purpose=purpose, used_at__isnull=True, expires_at__gt=timezone.now())
            .order_by("-created_at")
            .first()
        )
        if otp is None:
            return False
        if otp.attempts >= settings.OTP_MAX_ATTEMPTS:
            return False
        if not secrets.compare_digest(otp.code_hash, _hash_code((code or "").strip())):
            otp.attempts += 1
            otp.save(update_fields=["attempts"])
            return False
        otp.used_at = timezone.now()
        otp.save(update_fields=["used_at"])
        return True
