"""Доступ к заседанию: члены/наблюдатели комиссии или приглашённый участник."""
from django.core.exceptions import PermissionDenied
from django.shortcuts import get_object_or_404
from django.utils import timezone

from apps.committees import access as commission_access

from .models import Invitation, Meeting


def is_invited(user, meeting):
    return Invitation.objects.filter(meeting=meeting, user=user, valid_until__gte=timezone.localdate()).exists()


def can_view_meeting(user, meeting):
    if commission_access.can_view(user, meeting.commission):
        return True
    return user.is_authenticated and is_invited(user, meeting)


def get_meeting_or_403(user, pk, manage=False):
    meeting = get_object_or_404(Meeting.objects.select_related("commission"), pk=pk)
    if manage:
        allowed = commission_access.can_manage(user, meeting.commission)
    else:
        allowed = can_view_meeting(user, meeting)
    if not allowed:
        raise PermissionDenied("Нет доступа к заседанию")
    return meeting
