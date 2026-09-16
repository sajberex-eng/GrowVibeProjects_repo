from django.conf import settings
from django.contrib import messages
from django.contrib.auth import update_session_auth_hash
from django.contrib.auth import views as auth_views
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.core.mail import send_mail
from django.core.paginator import Paginator
from django.db.models import Q
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse_lazy
from django.views.decorators.http import require_POST

from apps.audit import services as audit

from .forms import LockoutAuthenticationForm, ProfileForm, UserAdminForm, generate_password
from .models import User


class LoginView(auth_views.LoginView):
    template_name = "accounts/login.html"
    authentication_form = LockoutAuthenticationForm
    redirect_authenticated_user = True

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["timeout"] = self.request.GET.get("timeout")
        return ctx


class PasswordChangeView(auth_views.PasswordChangeView):
    template_name = "accounts/password_change.html"
    success_url = reverse_lazy("dashboard")

    def form_valid(self, form):
        response = super().form_valid(form)
        user = form.user
        user.must_change_password = False
        user.save(update_fields=["must_change_password"])
        update_session_auth_hash(self.request, user)
        messages.success(self.request, "Пароль изменён.")
        return response


class PasswordResetView(auth_views.PasswordResetView):
    template_name = "accounts/password_reset.html"
    email_template_name = "accounts/password_reset_email.txt"
    subject_template_name = "accounts/password_reset_subject.txt"
    success_url = reverse_lazy("accounts:password_reset_done")
    extra_email_context = {"site_url": settings.SITE_URL}


class PasswordResetConfirmView(auth_views.PasswordResetConfirmView):
    template_name = "accounts/password_reset_confirm.html"
    success_url = reverse_lazy("accounts:password_reset_complete")

    def form_valid(self, form):
        response = super().form_valid(form)
        form.user.must_change_password = False
        form.user.locked_until = None
        form.user.failed_login_attempts = 0
        form.user.save(update_fields=["must_change_password", "locked_until", "failed_login_attempts"])
        return response


@login_required
def profile(request):
    from apps.committees.models import CommissionAccess, MemberRecord
    from apps.notifications.models import CRITICAL_EVENTS, Event, NotificationPreference

    user = request.user
    form = ProfileForm(request.POST or None, instance=user)
    prefs = {p.event: p for p in NotificationPreference.objects.filter(user=user)}
    if request.method == "POST" and form.is_valid():
        form.save()
        for event, _label in Event.choices:
            email = f"email_{event}" in request.POST or event in CRITICAL_EVENTS
            whatsapp = f"wa_{event}" in request.POST
            NotificationPreference.objects.update_or_create(
                user=user, event=event, defaults={"email": email, "whatsapp": whatsapp}
            )
        messages.success(request, "Профиль сохранён.")
        return redirect("accounts:profile")
    rows = []
    for event, label in Event.choices:
        pref = prefs.get(event)
        rows.append({
            "event": event,
            "label": label,
            "email": pref.email if pref else True,
            "whatsapp": pref.whatsapp if pref else True,
            "critical": event in CRITICAL_EVENTS,
        })
    records = MemberRecord.objects.filter(user=user).select_related("commission", "start_order", "end_order").order_by("-start_date")
    observed = CommissionAccess.objects.filter(user=user).select_related("commission")
    return render(request, "accounts/profile.html", {
        "form": form, "pref_rows": rows, "records": records, "observed": observed,
    })


def _require_admin(user):
    if not user.is_admin:
        raise PermissionDenied("Раздел доступен только администратору")


@login_required
def user_list(request):
    _require_admin(request.user)
    qs = User.objects.select_related("position", "department", "city").order_by("last_name", "first_name")
    q = request.GET.get("q", "").strip()
    if q:
        qs = qs.filter(Q(last_name__icontains=q) | Q(first_name__icontains=q) | Q(username__icontains=q) | Q(email__icontains=q))
    status = request.GET.get("status", "active")
    if status == "active":
        qs = qs.filter(is_active=True)
    elif status == "inactive":
        qs = qs.filter(is_active=False)
    page = Paginator(qs, 50).get_page(request.GET.get("page"))
    return render(request, "accounts/user_list.html", {"page": page, "q": q, "status": status})


def _send_credentials(request, user, password):
    send_mail(
        "Доступ к системе «Комиссии Центра гематологии»",
        (
            f"Здравствуйте, {user.first_name}!\n\n"
            f"Для вас создана учётная запись в системе: {settings.SITE_URL}/\n"
            f"Логин: {user.username}\nВременный пароль: {password}\n\n"
            "При первом входе система попросит сменить пароль."
        ),
        settings.DEFAULT_FROM_EMAIL,
        [user.email],
    )


@login_required
def user_edit(request, pk=None):
    _require_admin(request.user)
    user = get_object_or_404(User, pk=pk) if pk else None
    form = UserAdminForm(request.POST or None, instance=user)
    if request.method == "POST" and form.is_valid():
        obj = form.save(commit=False)
        if user is None:
            password = generate_password()
            obj.set_password(password)
            obj.must_change_password = True
            obj.save()
            _send_credentials(request, obj, password)
            messages.success(request, f"Пользователь создан. Временный пароль отправлен на {obj.email}.")
        else:
            obj.save()
            messages.success(request, "Изменения сохранены.")
        return redirect("accounts:user_list")
    return render(request, "accounts/user_form.html", {"form": form, "obj": user})


@login_required
@require_POST
def user_reset_password(request, pk):
    _require_admin(request.user)
    user = get_object_or_404(User, pk=pk)
    password = generate_password()
    user.set_password(password)
    user.must_change_password = True
    user.locked_until = None
    user.failed_login_attempts = 0
    user.save()
    _send_credentials(request, user, password)
    audit.log("password_reset_by_admin", user)
    messages.success(request, f"Новый временный пароль отправлен на {user.email}.")
    return redirect("accounts:user_edit", pk=pk)


@login_required
@require_POST
def user_unlock(request, pk):
    _require_admin(request.user)
    user = get_object_or_404(User, pk=pk)
    user.reset_failed_logins()
    audit.log("unlock", user)
    messages.success(request, "Учётная запись разблокирована.")
    return redirect("accounts:user_edit", pk=pk)
