from django.db.models.fields.files import FieldFile

from .context import current_ip, current_user
from .models import AuditLog

# Поля, которые не пишутся в журнал.
SKIP_FIELDS = {"password", "code_hash", "last_login", "failed_login_attempts"}


def _plain(value):
    if isinstance(value, FieldFile):
        return value.name or ""
    if hasattr(value, "isoformat"):
        return value.isoformat()
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def snapshot(instance):
    data = {}
    for field in instance._meta.concrete_fields:
        if field.name in SKIP_FIELDS:
            continue
        data[field.attname] = _plain(getattr(instance, field.attname))
    return data


def log(action, obj=None, details=None, user=None, ip=None):
    """Запись события в журнал аудита.

    action — короткий код действия: create, update, delete, login, sign, vote…
    """
    if user is None:
        user = current_user()
    if ip is None:
        ip = current_ip()
    entry = AuditLog(
        user=user if user is not None and user.pk else None,
        ip_address=ip,
        action=action,
        details=details or {},
    )
    if obj is not None:
        entry.object_type = f"{obj._meta.app_label}.{obj._meta.model_name}"
        entry.object_id = str(obj.pk)
        entry.object_repr = str(obj)[:300]
    entry.save()
    return entry
