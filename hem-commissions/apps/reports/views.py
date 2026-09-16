from urllib.parse import urlencode

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.db.models import Q
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from apps.audit.middleware import client_ip
from apps.committees import access
from apps.committees.services import check_signing_code, send_signing_code, serve_file

from . import services
from .forms import ReportForm, SignCodeForm
from .models import SignedReport

XLSX_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _resolve(request, data):
    """Возвращает (form, commissions, date_from, date_to); commissions=None при ошибке формы."""
    visible = access.visible_commissions(request.user)
    form = ReportForm(data, commissions=visible)
    if not form.is_valid():
        return form, None, None, None
    commission = form.cleaned_data.get("commission")
    commissions = [commission] if commission else list(visible)
    return form, commissions, form.cleaned_data["date_from"], form.cleaned_data["date_to"]


def _query(form):
    data = form.cleaned_data
    params = {"period": data["period"], "year": data["year"]}
    if data.get("commission"):
        params["commission"] = data["commission"].pk
    if data["period"] == "custom":
        params["date_from"] = data["date_from"].isoformat()
        params["date_to"] = data["date_to"].isoformat()
    return urlencode(params)


def _filename(report, ext):
    sections = report["commissions"]
    name = sections[0]["commission"].short_name if len(sections) == 1 else "all"
    return f"report_{name}_{report['date_from']:%Y%m%d}_{report['date_to']:%Y%m%d}.{ext}"


def _attachment(response, filename):
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    return response


@login_required
def index(request):
    form, commissions, date_from, date_to = _resolve(request, request.GET)
    report = None
    single = None
    can_sign = False
    if commissions is not None:
        report = services.build_report(commissions, date_from, date_to)
        if form.cleaned_data.get("commission"):
            single = form.cleaned_data["commission"]
            can_sign = access.is_chairman(request.user, single)
    return render(request, "reports/index.html", {
        "form": form,
        "report": report,
        "title": services.report_title(report) if report else "Отчёты",
        "period_label": services.period_label(report) if report else "",
        "query": _query(form) if report else "",
        "single": single,
        "can_sign": can_sign,
        "state_labels": services.ASSIGNMENT_STATES,
    })


@login_required
def export_xlsx(request):
    form, commissions, date_from, date_to = _resolve(request, request.GET)
    if commissions is None:
        messages.error(request, "Проверьте параметры отчёта.")
        return redirect("reports:index")
    report = services.build_report(commissions, date_from, date_to)
    response = HttpResponse(services.export_xlsx(report), content_type=XLSX_TYPE)
    return _attachment(response, _filename(report, "xlsx"))


@login_required
def export_pdf(request):
    form, commissions, date_from, date_to = _resolve(request, request.GET)
    if commissions is None:
        messages.error(request, "Проверьте параметры отчёта.")
        return redirect("reports:index")
    report = services.build_report(commissions, date_from, date_to)
    response = HttpResponse(services.export_pdf(report), content_type="application/pdf")
    return _attachment(response, _filename(report, "pdf"))


@login_required
@require_POST
def sign(request):
    """Подписание отчёта председателем: шаг 1 — отправка кода, шаг 2 — подтверждение."""
    form, commissions, date_from, date_to = _resolve(request, request.POST)
    if commissions is None or not form.cleaned_data.get("commission"):
        messages.error(request, "Подписать можно отчёт одной комиссии.")
        return redirect("reports:index")
    commission = form.cleaned_data["commission"]
    access.require(access.is_chairman(request.user, commission), "Подписать отчёт может только председатель комиссии")
    purpose = services.signing_purpose(commission, date_from, date_to)
    title = f"Отчёт о работе {commission.short_name} {date_from:%d.%m.%Y}–{date_to:%d.%m.%Y}"
    code_form = SignCodeForm()
    if request.POST.get("step") == "confirm":
        code_form = SignCodeForm(request.POST)
        if code_form.is_valid():
            try:
                check_signing_code(request.user, purpose, code_form.cleaned_data["code"])
            except ValidationError as exc:
                code_form.add_error("code", exc)
            else:
                signed = services.sign_report(commission, date_from, date_to, request.user, client_ip(request))
                messages.success(request, "Отчёт подписан простой электронной подписью.")
                return redirect(f"{reverse('reports:signed_list')}#r{signed.pk}")
    else:
        try:
            send_signing_code(request.user, purpose, title)
            messages.info(request, f"Код подтверждения отправлен на {request.user.email}.")
        except ValidationError as exc:
            for msg in exc.messages:
                messages.error(request, msg)
            return redirect(f"{reverse('reports:index')}?{_query(form)}")
    return render(request, "reports/sign.html", {
        "commission": commission,
        "date_from": date_from,
        "date_to": date_to,
        "code_form": code_form,
        "params": form.cleaned_data,
        "query": _query(form),
    })


def _visible_signed(user):
    qs = SignedReport.objects.select_related("commission", "signed_by")
    if user.is_admin:
        return qs
    ids = list(access.visible_commissions(user).values_list("pk", flat=True))
    return qs.filter(Q(commission_id__in=ids))


@login_required
def signed_list(request):
    return render(request, "reports/signed_list.html", {"reports": _visible_signed(request.user)})


@login_required
def signed_download(request, pk):
    signed = get_object_or_404(SignedReport.objects.select_related("commission"), pk=pk)
    if signed.commission is None:
        allowed = request.user.is_admin
    else:
        allowed = access.can_view(request.user, signed.commission)
    if not allowed:
        raise PermissionDenied("Нет доступа к отчёту")
    return serve_file(signed.pdf, f"{signed}.pdf".replace("/", "-"), inline=False)
