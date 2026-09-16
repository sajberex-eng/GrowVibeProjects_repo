"""Разграничение доступа по комиссиям.

Роли пользователя в комиссии определяются действующим составом (MemberRecord),
доступом наблюдателя (CommissionAccess) и флагом администратора (is_staff).
Приглашённые участники получают доступ только к конкретному заседанию
(см. apps.meetings.access).
"""
from django.core.exceptions import PermissionDenied
from django.shortcuts import get_object_or_404

from .models import Commission, CommissionAccess, MemberRecord

ADMIN = "admin"
CHAIRMAN = MemberRecord.Role.CHAIRMAN
DEPUTY = MemberRecord.Role.DEPUTY
SECRETARY = MemberRecord.Role.SECRETARY
MEMBER = MemberRecord.Role.MEMBER
OBSERVER = "observer"

ROLE_LABELS = {
    ADMIN: "Администратор",
    CHAIRMAN: "Председатель",
    DEPUTY: "Заместитель председателя",
    SECRETARY: "Секретарь",
    MEMBER: "Член комиссии",
    OBSERVER: "Руководитель (наблюдатель)",
}


def roles_in(user, commission, on_date=None):
    """Множество ролей пользователя в комиссии."""
    if not user.is_authenticated:
        return set()
    cache = getattr(user, "_commission_roles_cache", None)
    key = (commission.pk, on_date)
    if cache is None:
        cache = user._commission_roles_cache = {}
    if key in cache:
        return cache[key]
    roles = set(commission.members_on(on_date).filter(user=user).values_list("role", flat=True))
    if CommissionAccess.objects.filter(commission=commission, user=user).exists():
        roles.add(OBSERVER)
    if user.is_admin:
        roles.add(ADMIN)
    cache[key] = roles
    return roles


def can_view(user, commission):
    return bool(roles_in(user, commission))


def can_manage(user, commission):
    """Секретарь, председатель или администратор — ведение комиссии."""
    return bool(roles_in(user, commission) & {ADMIN, SECRETARY, CHAIRMAN})


def is_secretary(user, commission):
    return bool(roles_in(user, commission) & {ADMIN, SECRETARY})


def is_chairman(user, commission):
    return CHAIRMAN in roles_in(user, commission)


def is_voting_member(user, commission, on_date=None):
    return bool(roles_in(user, commission, on_date) & {CHAIRMAN, DEPUTY, SECRETARY, MEMBER})


def visible_commissions(user):
    qs = Commission.objects.filter(is_active=True)
    if user.is_admin:
        return qs
    ids = {c.pk for c in qs if can_view(user, c)}
    return qs.filter(pk__in=ids)


def get_commission_or_403(user, pk, manage=False):
    commission = get_object_or_404(Commission, pk=pk)
    allowed = can_manage(user, commission) if manage else can_view(user, commission)
    if not allowed:
        raise PermissionDenied("Нет доступа к комиссии")
    return commission


def require(condition, message="Недостаточно прав"):
    if not condition:
        raise PermissionDenied(message)
