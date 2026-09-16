"""Автоматическая запись изменений доменных моделей в журнал аудита."""
from django.contrib.auth.signals import user_logged_in, user_logged_out, user_login_failed
from django.db.models.signals import post_delete, post_save, pre_save
from django.dispatch import receiver

from . import services

AUDITED_APPS = {"accounts", "committees", "meetings", "protocols", "voting"}
SKIP_MODELS = {"onetimecode"}


def _audited(sender):
    meta = sender._meta
    return meta.app_label in AUDITED_APPS and meta.model_name not in SKIP_MODELS


@receiver(pre_save)
def remember_old_state(sender, instance, **kwargs):
    if not _audited(sender) or not instance.pk:
        return
    try:
        old = sender._default_manager.get(pk=instance.pk)
    except sender.DoesNotExist:
        return
    instance._audit_old = services.snapshot(old)


@receiver(post_save)
def log_save(sender, instance, created, raw=False, **kwargs):
    if raw or not _audited(sender):
        return
    new = services.snapshot(instance)
    if created:
        services.log("create", instance, {"values": new})
        return
    old = getattr(instance, "_audit_old", {})
    changes = {k: [old.get(k), v] for k, v in new.items() if old.get(k) != v}
    if changes:
        services.log("update", instance, {"changes": changes})


@receiver(post_delete)
def log_delete(sender, instance, **kwargs):
    if _audited(sender):
        services.log("delete", instance, {"values": services.snapshot(instance)})


@receiver(user_logged_in)
def log_login(sender, request, user, **kwargs):
    services.log("login", user, user=user)


@receiver(user_logged_out)
def log_logout(sender, request, user, **kwargs):
    if user is not None:
        services.log("logout", user, user=user)


@receiver(user_login_failed)
def log_login_failed(sender, credentials, request=None, **kwargs):
    services.log("login_failed", details={"username": credentials.get("username", "")})
