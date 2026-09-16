from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db.models import Case, IntegerField, Value, When
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST

from .models import Notification


@login_required
def notification_list(request):
    qs = request.user.notifications.select_related("commission")
    unread_only = request.GET.get("unread") == "1"
    if unread_only:
        qs = qs.filter(read_at__isnull=True)
    qs = qs.annotate(
        unread_first=Case(When(read_at__isnull=True, then=Value(0)), default=Value(1), output_field=IntegerField())
    ).order_by("unread_first", "-created_at")
    page = Paginator(qs, 30).get_page(request.GET.get("page"))
    return render(request, "notifications/list.html", {"page": page, "unread_only": unread_only})


@login_required
def notification_read(request, pk):
    note = get_object_or_404(Notification, pk=pk, user=request.user)
    if note.read_at is None:
        note.read_at = timezone.now()
        note.save(update_fields=["read_at"])
    if note.url and url_has_allowed_host_and_scheme(
        note.url, allowed_hosts={request.get_host()}, require_https=request.is_secure()
    ):
        return redirect(note.url)
    return redirect("notifications:list")


@login_required
@require_POST
def notification_read_all(request):
    request.user.notifications.filter(read_at__isnull=True).update(read_at=timezone.now())
    return redirect("notifications:list")
