"""Комиссии: реестр, карточка, состав, приказы, положения, НПА, настройки."""
from datetime import date

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.db import transaction
from django.core.exceptions import PermissionDenied
from django.db.models import Count, Max, ProtectedError, Q
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST

from apps.audit import services as audit

from . import access, adilet, docgen
from .forms import (
    CommissionForm, CommissionSettingsForm, CompositionChangeForm, ExpireForm, LegalActForm,
    ObserverForm, OrderDraftForm, OrderRegisterForm, RegulationForm, RubricForm,
)
from .models import (
    Commission, CommissionAccess, CompositionChange, LegalAct, LegalActCheck, MemberRecord, Order, Regulation, Rubric,
)
from .services import composition_diff, register_signed_order, serve_file

TABS = [
    ("overview", "Обзор"),
    ("composition", "Состав"),
    ("orders", "Приказы"),
    ("regulations", "Положение"),
    ("legal", "Нормативная база"),
    ("settings", "Настройки"),
]
KIND_TITLES = {
    Commission.Kind.CORE: "Основные комиссии",
    Commission.Kind.REQUIRED_IF: "Требуются НПА при наличии деятельности",
    Commission.Kind.RECOMMENDED: "Рекомендуемые (создаются приказом руководителя)",
}
DOT = {
    LegalAct.Status.ACTUAL: "green",
    LegalAct.Status.LOST_FORCE: "red",
    LegalAct.Status.UNAVAILABLE: "red",
    LegalAct.Status.NEEDS_CHECK: "amber",
    LegalAct.Status.RETIRED: "gray",
}


# ---------- помощники ----------

def _admin_required(user):
    access.require(user.is_admin, "Действие доступно только администратору")


def _tab_url(commission, tab, **params):
    url = reverse("committees:detail", args=[commission.pk]) + f"?tab={tab}"
    for k, v in params.items():
        url += f"&{k}={v}"
    return url


def _parse_date(value):
    try:
        return date.fromisoformat(value) if value else None
    except ValueError:
        return None


def legal_indicator(acts):
    """Сводный индикатор НПА комиссии: (цвет, подпись, дата последней проверки)."""
    acts = [a for a in acts if a.status != LegalAct.Status.RETIRED]
    if not acts:
        return {"color": "gray", "label": "НПА не указаны", "checked": None}
    statuses = {a.status for a in acts}
    checked = [a.last_checked_at for a in acts if a.last_checked_at]
    last = max(checked) if checked else None
    if statuses & {LegalAct.Status.LOST_FORCE, LegalAct.Status.UNAVAILABLE}:
        color, label = "red", "Есть утратившие силу или недоступные НПА"
    elif LegalAct.Status.NEEDS_CHECK in statuses:
        color, label = "amber", "Есть НПА, требующие проверки"
    else:
        color, label = "green", "Все НПА актуальны"
    return {"color": color, "label": label, "checked": last}


def _annotate_acts(acts):
    for a in acts:
        a.dot = DOT.get(a.status, "gray")
    return acts


def current_regulation(commission, on_date=None):
    on_date = on_date or timezone.localdate()
    return (
        commission.regulations.filter(approved_on__lte=on_date)
        .filter(Q(expired_on__isnull=True) | Q(expired_on__gt=on_date))
        .order_by("-approved_on", "-version")
        .first()
    )


def composition_order(commission, on_date=None):
    """Последний подписанный приказ, изменивший состав на дату."""
    on_date = on_date or timezone.localdate()
    return (
        commission.orders.filter(status=Order.Status.SIGNED, signed_on__lte=on_date)
        .filter(Q(started_records__isnull=False) | Q(ended_records__isnull=False))
        .distinct()
        .order_by("-signed_on", "-created_at")
        .first()
    )


# ---------- реестр ----------

@login_required
def commission_list(request):
    user = request.user
    show_inactive = user.is_admin and request.GET.get("all") == "1"
    if user.is_admin:
        qs = Commission.objects.all() if show_inactive else Commission.objects.filter(is_active=True)
    else:
        qs = access.visible_commissions(user)
    today = timezone.localdate()
    qs = qs.prefetch_related("cities", "legal_acts").annotate(
        members_count=Count(
            "member_records",
            filter=Q(member_records__start_date__lte=today)
            & (Q(member_records__end_date__isnull=True) | Q(member_records__end_date__gt=today)),
            distinct=True,
        )
    )
    groups = []
    for kind, title in KIND_TITLES.items():
        items = [c for c in qs if c.kind == kind]
        for c in items:
            c.legal = legal_indicator(c.legal_acts.all())
            c.my_roles = sorted(access.ROLE_LABELS.get(r, r) for r in access.roles_in(user, c) if r != access.ADMIN)
        if items:
            groups.append({"kind": kind, "title": title, "items": items})
    view = request.GET.get("view", "cards")
    return render(request, "committees/list.html", {
        "groups": groups, "show_inactive": show_inactive, "view": view,
    })


@login_required
def commission_edit(request, pk=None):
    _admin_required(request.user)
    commission = get_object_or_404(Commission, pk=pk) if pk else None
    form = CommissionForm(request.POST or None, instance=commission)
    if request.method == "POST" and form.is_valid():
        commission = form.save()
        messages.success(request, "Комиссия сохранена.")
        return redirect("committees:detail", pk=commission.pk)
    return render(request, "committees/commission_form.html", {"form": form, "commission": commission})


# ---------- карточка ----------

@login_required
def commission_detail(request, pk):
    commission = access.get_commission_or_403(request.user, pk)
    user = request.user
    manage = access.can_manage(user, commission)
    tab = request.GET.get("tab", "overview")
    if tab not in dict(TABS):
        tab = "overview"
    if tab == "settings" and not manage:
        raise PermissionDenied("Настройки доступны секретарю, председателю и администратору")
    tabs = [(k, v) for k, v in TABS if k != "settings" or manage]
    ctx = {
        "commission": commission,
        "tab": tab,
        "tabs": tabs,
        "manage": manage,
        "is_admin": user.is_admin,
        "my_roles": sorted(access.ROLE_LABELS.get(r, r) for r in access.roles_in(user, commission)),
        "today": timezone.localdate(),
    }
    builder = {
        "overview": _overview_ctx,
        "composition": _composition_ctx,
        "orders": _orders_ctx,
        "regulations": _regulations_ctx,
        "legal": _legal_ctx,
        "settings": _settings_ctx,
    }[tab]
    ctx.update(builder(request, commission, manage))
    return render(request, "committees/detail.html", ctx)


def _overview_ctx(request, commission, manage):
    from apps.meetings.models import Meeting
    from apps.protocols.models import Assignment, Protocol

    now = timezone.now()
    acts = _annotate_acts(list(commission.legal_acts.exclude(status=LegalAct.Status.RETIRED)))
    protocols = Protocol.objects.filter(commission=commission).exclude(status=Protocol.Status.ANNULLED)
    if not manage:
        protocols = protocols.filter(status=Protocol.Status.SIGNED)
    return {
        "members": commission.members_on(),
        "regulation": current_regulation(commission),
        "comp_order": composition_order(commission),
        "acts": acts,
        "legal": legal_indicator(acts),
        "upcoming": Meeting.objects.filter(
            commission=commission, starts_at__gte=now,
            status__in=[Meeting.Status.PLANNED, Meeting.Status.POSTPONED],
        ).order_by("starts_at")[:5],
        "assignments": Assignment.objects.filter(
            commission=commission, status__in=Assignment.OPEN_STATUSES
        ).select_related("responsible").order_by("due_date")[:10],
        "protocols": protocols.order_by("-protocol_date", "-id")[:5],
        "pending_changes": commission.composition_changes.filter(status=CompositionChange.Status.PROJECT).count(),
    }


def _composition_ctx(request, commission, manage):
    today = timezone.localdate()
    on = _parse_date(request.GET.get("on"))
    a, b = _parse_date(request.GET.get("a")), _parse_date(request.GET.get("b"))
    ctx = {
        "on": on,
        "members": commission.members_on(on or today).select_related("start_order"),
        "basis_order": composition_order(commission, on or today),
        "orders_changed_on": commission.orders.filter(status=Order.Status.SIGNED).order_by("-signed_on"),
    }
    if a and b:
        ctx["diff"] = composition_diff(commission, a, b)
        ctx["diff_a"], ctx["diff_b"] = a, b
    if manage:
        ctx["pending"] = (
            commission.composition_changes.filter(status=CompositionChange.Status.PROJECT)
            .select_related("user", "user__position", "order")
            .order_by("created_at")
        )
        ctx["change_form"] = CompositionChangeForm(commission=commission)
        ctx["order_form"] = OrderDraftForm()
    return ctx


def _orders_ctx(request, commission, manage):
    orders = commission.orders.annotate(changes_count=Count("changes")).select_related("created_by")
    if not manage:
        orders = orders.filter(status=Order.Status.SIGNED)
    orders = list(orders.order_by("-created_at"))
    for o in orders:
        o.change_items = list(o.changes.select_related("user"))
    return {"orders": orders}


def _regulations_ctx(request, commission, manage):
    return {
        "regulations": commission.regulations.select_related("uploaded_by").order_by("-version"),
        "current": current_regulation(commission),
        "regulation_form": RegulationForm(commission=commission) if manage else None,
    }


def _legal_ctx(request, commission, manage):
    acts = _annotate_acts(list(commission.legal_acts.select_related("replaced_by")))
    return {
        "acts": acts,
        "legal": legal_indicator(acts),
        "checks": LegalActCheck.objects.filter(act__commission=commission)
        .select_related("act", "initiated_by")[:15],
        "act_form": LegalActForm() if manage else None,
        "playwright": adilet.playwright_available(),
    }


def _settings_ctx(request, commission, manage):
    ctx = {
        "settings_form": CommissionSettingsForm(instance=commission),
        "rubrics": commission.rubrics.all(),
        "rubric_form": RubricForm(commission=commission),
    }
    if request.user.is_admin:
        ctx["observers"] = commission.observers.select_related("user", "granted_by")
        ctx["observer_form"] = ObserverForm(commission=commission)
    return ctx


def _render_tab_with_form(request, commission, tab, **forms):
    """Повторный показ вкладки с ошибками формы."""
    manage = access.can_manage(request.user, commission)
    ctx = {
        "commission": commission, "tab": tab,
        "tabs": [(k, v) for k, v in TABS if k != "settings" or manage],
        "manage": manage, "is_admin": request.user.is_admin,
        "my_roles": sorted(access.ROLE_LABELS.get(r, r) for r in access.roles_in(request.user, commission)),
        "today": timezone.localdate(),
    }
    builder = {
        "composition": _composition_ctx, "regulations": _regulations_ctx,
        "legal": _legal_ctx, "settings": _settings_ctx,
    }[tab]
    ctx.update(builder(request, commission, manage))
    ctx.update(forms)
    return render(request, "committees/detail.html", ctx, status=400)


# ---------- состав ----------

@login_required
@require_POST
def change_add(request, pk):
    commission = access.get_commission_or_403(request.user, pk, manage=True)
    form = CompositionChangeForm(request.POST, commission=commission)
    if not form.is_valid():
        return _render_tab_with_form(request, commission, "composition", change_form=form)
    change = form.save(commit=False)
    change.commission = commission
    change.created_by = request.user
    change.save()
    messages.success(request, f"Изменение добавлено в проект состава: {change}.")
    return redirect(_tab_url(commission, "composition"))


@login_required
@require_POST
def change_cancel(request, pk, change_id):
    commission = access.get_commission_or_403(request.user, pk, manage=True)
    change = get_object_or_404(
        CompositionChange, pk=change_id, commission=commission, status=CompositionChange.Status.PROJECT
    )
    if change.order_id and change.order.status == Order.Status.DRAFT:
        change.order = None
    change.status = CompositionChange.Status.CANCELLED
    change.save()
    messages.info(request, f"Изменение отменено: {change}.")
    return redirect(_tab_url(commission, "composition"))


# ---------- приказы ----------

def _get_order(request, pk, order_id, manage=False):
    commission = access.get_commission_or_403(request.user, pk, manage=manage)
    order = get_object_or_404(Order, pk=order_id, commission=commission)
    return commission, order


@login_required
@require_POST
def order_create(request, pk):
    commission = access.get_commission_or_403(request.user, pk, manage=True)
    ids = request.POST.getlist("changes")
    changes = list(
        commission.composition_changes.filter(pk__in=ids, status=CompositionChange.Status.PROJECT)
        .filter(Q(order__isnull=True) | Q(order__status=Order.Status.CANCELLED))
        .select_related("user")
    )
    form = OrderDraftForm(request.POST)
    if not changes:
        messages.error(request, "Отметьте изменения состава, не включённые в другой проект приказа.")
        return redirect(_tab_url(commission, "composition"))
    if not form.is_valid():
        return _render_tab_with_form(request, commission, "composition", order_form=form)
    with transaction.atomic():
        order = Order.objects.create(
            commission=commission, title=form.cleaned_data["title"], preamble=form.cleaned_data["preamble"],
            status=Order.Status.DRAFT, created_by=request.user,
        )
        CompositionChange.objects.filter(pk__in=[c.pk for c in changes]).update(order=order)
        docgen.generate_order_draft(order)
    messages.success(request, "Проект приказа сформирован. Скачайте DOCX, подпишите и загрузите скан.")
    return redirect(_tab_url(commission, "orders"))


@login_required
@require_POST
def order_regenerate(request, pk, order_id):
    commission, order = _get_order(request, pk, order_id, manage=True)
    access.require(order.status == Order.Status.DRAFT, "Приказ уже подписан или отменён")
    docgen.generate_order_draft(order)
    messages.success(request, "Проект приказа сформирован заново.")
    return redirect(_tab_url(commission, "orders"))


@login_required
def order_draft(request, pk, order_id):
    commission, order = _get_order(request, pk, order_id, manage=True)
    if not order.draft_file:
        raise Http404("Проект не сформирован")
    audit.log("download", order, {"file": "draft"})
    return serve_file(order.draft_file, f"Проект приказа {commission.short_name} {order.pk}.docx", inline=False)


@login_required
def order_scan(request, pk, order_id):
    commission, order = _get_order(request, pk, order_id)
    if not order.scan_file:
        raise Http404("Скан не загружен")
    if order.status != Order.Status.SIGNED:
        access.require(access.can_manage(request.user, commission))
    audit.log("download", order, {"file": "scan"})
    name = f"Приказ {commission.short_name} № {order.number or order.pk}.pdf".replace("/", "-")
    return serve_file(order.scan_file, name)


@login_required
def order_register(request, pk, order_id):
    commission, order = _get_order(request, pk, order_id, manage=True)
    access.require(order.status == Order.Status.DRAFT, "Регистрировать можно только проект приказа")
    form = OrderRegisterForm(request.POST or None, request.FILES or None)
    if request.method == "POST" and form.is_valid():
        d = form.cleaned_data
        try:
            register_signed_order(order, d["number"], d["signed_on"], d["scan_file"])
        except ValidationError as exc:
            order.refresh_from_db()
            form.add_error(None, exc)
        else:
            audit.log("register_order", order, {"number": d["number"], "signed_on": d["signed_on"].isoformat()})
            _notify_composition(commission, order)
            messages.success(request, f"Приказ зарегистрирован. Изменения состава действуют с {d['signed_on']:%d.%m.%Y}.")
            return redirect(_tab_url(commission, "composition"))
    changes = order.changes.select_related("user")
    return render(request, "committees/order_register.html", {
        "commission": commission, "order": order, "form": form, "changes": changes,
    })


def _notify_composition(commission, order):
    from apps.notifications.models import Event
    from apps.notifications.services import notify

    users = set(commission.member_users_on(order.signed_on))
    users.update(ch.user for ch in order.changes.select_related("user"))
    secretary = commission.officer(MemberRecord.Role.SECRETARY)
    if secretary:
        users.add(secretary)
    lines = [f"• {ch.get_action_display()}: {ch.user}" for ch in order.changes.select_related("user")]
    notify(
        list(users), Event.COMPOSITION,
        f"Изменён состав комиссии {commission.short_name}",
        f"{order} вступил в силу с {order.signed_on:%d.%m.%Y}.\n" + "\n".join(lines),
        _tab_url(commission, "composition"), commission,
    )


@login_required
@require_POST
def order_cancel(request, pk, order_id):
    commission, order = _get_order(request, pk, order_id, manage=True)
    access.require(order.status == Order.Status.DRAFT, "Отменить можно только проект приказа")
    order.status = Order.Status.CANCELLED
    order.save()
    for change in order.changes.filter(status=CompositionChange.Status.PROJECT):
        change.order = None
        change.save()
    messages.info(request, "Проект приказа отменён; изменения возвращены в проект состава.")
    return redirect(_tab_url(commission, "orders"))


# ---------- положения ----------

@login_required
@require_POST
def regulation_upload(request, pk):
    commission = access.get_commission_or_403(request.user, pk, manage=True)
    form = RegulationForm(request.POST, request.FILES, commission=commission)
    if not form.is_valid():
        return _render_tab_with_form(request, commission, "regulations", regulation_form=form)
    with transaction.atomic():
        reg = form.save(commit=False)
        reg.commission = commission
        reg.uploaded_by = request.user
        reg.version = (commission.regulations.aggregate(m=Max("version"))["m"] or 0) + 1
        for old in commission.regulations.filter(expired_on__isnull=True):
            old.expired_on = reg.approved_on
            old.save()
        reg.save()
    messages.success(request, f"Загружена редакция {reg.version} положения.")
    return redirect(_tab_url(commission, "regulations"))


@login_required
def regulation_download(request, pk, reg_id):
    commission = access.get_commission_or_403(request.user, pk)
    reg = get_object_or_404(Regulation, pk=reg_id, commission=commission)
    ext = reg.file.name.rsplit(".", 1)[-1] if "." in reg.file.name else "pdf"
    return serve_file(reg.file, f"Положение {commission.short_name} ред. {reg.version}.{ext}")


@login_required
@require_POST
def regulation_expire(request, pk, reg_id):
    commission = access.get_commission_or_403(request.user, pk, manage=True)
    reg = get_object_or_404(Regulation, pk=reg_id, commission=commission, expired_on__isnull=True)
    form = ExpireForm(request.POST)
    if form.is_valid() and form.cleaned_data["expired_on"] >= reg.approved_on:
        reg.expired_on = form.cleaned_data["expired_on"]
        reg.save()
        messages.info(request, f"Редакция {reg.version} отмечена как утратившая силу.")
    else:
        messages.error(request, "Укажите корректную дату (не ранее даты утверждения).")
    return redirect(_tab_url(commission, "regulations"))


# ---------- НПА ----------

def _get_act(request, pk, act_id):
    commission = access.get_commission_or_403(request.user, pk, manage=True)
    return commission, get_object_or_404(LegalAct, pk=act_id, commission=commission)


@login_required
def legal_act_edit(request, pk, act_id=None):
    commission = access.get_commission_or_403(request.user, pk, manage=True)
    act = get_object_or_404(LegalAct, pk=act_id, commission=commission) if act_id else None
    form = LegalActForm(request.POST or None, instance=act)
    if request.method == "POST" and form.is_valid():
        obj = form.save(commit=False)
        obj.commission = commission
        if act and "url" in form.changed_data and obj.status != LegalAct.Status.RETIRED:
            obj.status = LegalAct.Status.NEEDS_CHECK
        obj.save()
        messages.success(request, "НПА сохранён.")
        return redirect(_tab_url(commission, "legal"))
    return render(request, "committees/legal_act_form.html", {
        "commission": commission, "act": act, "form": form, "mode": "edit" if act else "add",
    })


@login_required
@require_POST
def legal_act_check(request, pk, act_id):
    commission, act = _get_act(request, pk, act_id)
    check = adilet.check_act(act, manual=True, user=request.user)
    messages.info(request, f"Проверка «{act}»: {check.get_result_display()}. {check.details}")
    target = request.POST.get("next", "")
    if not url_has_allowed_host_and_scheme(target, allowed_hosts={request.get_host()}, require_https=request.is_secure()):
        target = _tab_url(commission, "legal")
    return redirect(target)


@login_required
@require_POST
def legal_check_commission(request, pk):
    commission = access.get_commission_or_403(request.user, pk, manage=True)
    counts = adilet.check_acts(adilet.checkable_acts(commission), manual=True, user=request.user)
    messages.info(request, _counts_message(counts))
    return redirect(_tab_url(commission, "legal"))


def _counts_message(counts):
    if not counts.get("total"):
        return "Нет НПА со ссылкой для проверки."
    parts = [
        f"{LegalAct.Status(k).label}: {v}" for k, v in counts.items() if k != "total"
    ]
    return f"Проверено НПА: {counts['total']}. " + "; ".join(parts)


@login_required
@require_POST
def legal_act_retire(request, pk, act_id):
    commission, act = _get_act(request, pk, act_id)
    act.status = LegalAct.Status.RETIRED
    act.save()
    audit.log("legal_act_retire", act)
    messages.info(request, f"«{act}» отмечен: утратил силу, замена не требуется.")
    return redirect(_tab_url(commission, "legal"))


@login_required
def legal_act_replace(request, pk, act_id):
    commission, act = _get_act(request, pk, act_id)
    access.require(act.replaced_by_id is None, "НПА уже заменён")
    form = LegalActForm(request.POST or None, initial={"authority": act.authority, "sort": act.sort})
    if request.method == "POST" and form.is_valid():
        with transaction.atomic():
            new = form.save(commit=False)
            new.commission = commission
            new.status = LegalAct.Status.NEEDS_CHECK
            new.save()
            act.replaced_by = new
            act.status = LegalAct.Status.LOST_FORCE
            act.save()
        messages.success(request, f"«{act}» заменён на «{new}».")
        return redirect(_tab_url(commission, "legal"))
    return render(request, "committees/legal_act_form.html", {
        "commission": commission, "act": act, "form": form, "mode": "replace",
    })


@login_required
def legal_act_log(request, pk, act_id):
    commission = access.get_commission_or_403(request.user, pk)
    act = get_object_or_404(LegalAct, pk=act_id, commission=commission)
    act.dot = DOT.get(act.status, "gray")
    return render(request, "committees/legal_act_log.html", {
        "commission": commission, "act": act,
        "checks": act.checks.select_related("initiated_by")[:100],
        "manage": access.can_manage(request.user, commission),
    })


@login_required
def legal_monitor(request):
    _admin_required(request.user)
    acts = LegalAct.objects.select_related("commission", "replaced_by").order_by("commission__short_name", "sort", "id")
    status = request.GET.get("status", "")
    commission_id = request.GET.get("commission", "")
    if status == "attention":
        acts = acts.filter(status__in=[LegalAct.Status.LOST_FORCE, LegalAct.Status.UNAVAILABLE, LegalAct.Status.NEEDS_CHECK])
    elif status in LegalAct.Status.values:
        acts = acts.filter(status=status)
    if commission_id.isdigit():
        acts = acts.filter(commission_id=int(commission_id))
    acts = _annotate_acts(list(acts))
    counts = dict(LegalAct.objects.values_list("status").annotate(n=Count("id")).values_list("status", "n"))
    summary = [
        {"code": code, "label": label, "count": counts.get(code, 0), "dot": DOT.get(code, "gray")}
        for code, label in LegalAct.Status.choices
    ]
    return render(request, "committees/legal_monitor.html", {
        "acts": acts,
        "summary": summary,
        "status": status,
        "commission_id": commission_id,
        "statuses": LegalAct.Status.choices,
        "commissions": Commission.objects.order_by("short_name"),
        "checks": LegalActCheck.objects.select_related("act", "act__commission", "initiated_by")[:30],
        "playwright": adilet.playwright_available(),
    })


@login_required
@require_POST
def legal_check_all(request):
    _admin_required(request.user)
    counts = adilet.check_acts(adilet.checkable_acts(), manual=True, user=request.user)
    messages.info(request, _counts_message(counts))
    return redirect("committees:legal_monitor")


# ---------- настройки ----------

@login_required
@require_POST
def settings_edit(request, pk):
    commission = access.get_commission_or_403(request.user, pk, manage=True)
    form = CommissionSettingsForm(request.POST, instance=commission)
    if not form.is_valid():
        return _render_tab_with_form(request, commission, "settings", settings_form=form)
    form.save()
    messages.success(request, "Настройки комиссии сохранены.")
    return redirect(_tab_url(commission, "settings"))


@login_required
def rubric_edit(request, pk, rubric_id=None):
    commission = access.get_commission_or_403(request.user, pk, manage=True)
    rubric = get_object_or_404(Rubric, pk=rubric_id, commission=commission) if rubric_id else None
    form = RubricForm(request.POST or None, instance=rubric, commission=commission)
    if request.method == "POST" and form.is_valid():
        obj = form.save(commit=False)
        obj.commission = commission
        obj.save()
        messages.success(request, "Рубрика сохранена.")
        return redirect(_tab_url(commission, "settings"))
    if request.method == "POST" and rubric is None:
        return _render_tab_with_form(request, commission, "settings", rubric_form=form)
    return render(request, "committees/rubric_form.html", {"commission": commission, "form": form, "rubric": rubric})


@login_required
@require_POST
def rubric_delete(request, pk, rubric_id):
    commission = access.get_commission_or_403(request.user, pk, manage=True)
    rubric = get_object_or_404(Rubric, pk=rubric_id, commission=commission)
    try:
        rubric.delete()
    except ProtectedError:
        messages.error(request, "Рубрика используется в вопросах повестки — удалить нельзя, переименуйте её.")
    else:
        messages.info(request, "Рубрика удалена.")
    return redirect(_tab_url(commission, "settings"))


@login_required
@require_POST
def observer_grant(request, pk):
    _admin_required(request.user)
    commission = get_object_or_404(Commission, pk=pk)
    form = ObserverForm(request.POST, commission=commission)
    if not form.is_valid():
        return _render_tab_with_form(request, commission, "settings", observer_form=form)
    CommissionAccess.objects.create(commission=commission, user=form.cleaned_data["user"], granted_by=request.user)
    messages.success(request, f"Доступ наблюдателя предоставлен: {form.cleaned_data['user']}.")
    return redirect(_tab_url(commission, "settings"))


@login_required
@require_POST
def observer_revoke(request, pk, access_id):
    _admin_required(request.user)
    commission = get_object_or_404(Commission, pk=pk)
    grant = get_object_or_404(CommissionAccess, pk=access_id, commission=commission)
    grant.delete()
    messages.info(request, f"Доступ наблюдателя отозван: {grant.user}.")
    return redirect(_tab_url(commission, "settings"))
