import json
from datetime import date, datetime, time, timedelta

from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.db.models import Q
from django.shortcuts import get_object_or_404, render
from django.utils import timezone
from django.views.decorators.http import require_GET

from .models import AuditLog


def _require_admin(user):
    if not user.is_admin:
        raise PermissionDenied("Журнал аудита доступен только администратору")


def _parse_date(value):
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError):
        return None


@login_required
@require_GET
def audit_list(request):
    _require_admin(request.user)
    qs = AuditLog.objects.select_related("user").order_by("-created_at")
    params = request.GET
    user_id = params.get("user", "")
    action = params.get("action", "")
    object_type = params.get("object_type", "")
    q = params.get("q", "").strip()
    date_from = _parse_date(params.get("date_from"))
    date_to = _parse_date(params.get("date_to"))
    if user_id.isdigit():
        qs = qs.filter(user_id=int(user_id))
    if action:
        qs = qs.filter(action=action)
    if object_type:
        qs = qs.filter(object_type=object_type)
    if date_from:
        qs = qs.filter(created_at__gte=timezone.make_aware(datetime.combine(date_from, time.min)))
    if date_to:
        qs = qs.filter(created_at__lt=timezone.make_aware(datetime.combine(date_to + timedelta(days=1), time.min)))
    if q:
        qs = qs.filter(
            Q(object_repr__icontains=q) | Q(object_id=q) | Q(ip_address__startswith=q)
            | Q(user__last_name__icontains=q) | Q(user__username__icontains=q)
        )
    page = Paginator(qs, 50).get_page(params.get("page"))
    user_ids = AuditLog.objects.exclude(user__isnull=True).values_list("user_id", flat=True).distinct()
    users = get_user_model().objects.filter(pk__in=user_ids).order_by("last_name", "first_name")
    return render(request, "audit/list.html", {
        "page": page,
        "users": users,
        "actions": AuditLog.objects.order_by("action").values_list("action", flat=True).distinct(),
        "object_types": AuditLog.objects.exclude(object_type="").order_by("object_type")
        .values_list("object_type", flat=True).distinct(),
        "f": {
            "user": user_id, "action": action, "object_type": object_type, "q": q,
            "date_from": date_from.isoformat() if date_from else "",
            "date_to": date_to.isoformat() if date_to else "",
        },
    })


@login_required
@require_GET
def audit_detail(request, pk):
    _require_admin(request.user)
    entry = get_object_or_404(AuditLog.objects.select_related("user"), pk=pk)
    return render(request, "audit/detail.html", {
        "entry": entry,
        "details_json": json.dumps(entry.details, ensure_ascii=False, indent=2, sort_keys=True, default=str),
    })
