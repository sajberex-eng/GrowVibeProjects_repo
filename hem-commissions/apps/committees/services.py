"""Общие сервисы: файлы, PDF-шрифты, подтверждение кодом, состав комиссии."""
import hashlib
import mimetypes
import os
from functools import lru_cache

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.mail import send_mail
from django.db import transaction
from django.http import FileResponse, HttpResponseRedirect

from apps.accounts.models import OneTimeCode

from .models import CompositionChange, MemberRecord, Order

ALLOWED_UPLOAD_EXTENSIONS = {
    "pdf", "doc", "docx", "xls", "xlsx", "ppt", "pptx", "odt", "ods", "odp", "rtf", "txt", "csv",
    "jpg", "jpeg", "png", "gif", "webp", "zip",
}


# ---------- файлы ----------

def validate_upload(uploaded, allowed=None):
    allowed = allowed or ALLOWED_UPLOAD_EXTENSIONS
    ext = uploaded.name.rsplit(".", 1)[-1].lower() if "." in uploaded.name else ""
    if ext not in allowed:
        raise ValidationError(f"Недопустимый тип файла: .{ext}")
    if uploaded.size > settings.MAX_UPLOAD_MB * 1024 * 1024:
        raise ValidationError(f"Файл больше {settings.MAX_UPLOAD_MB} МБ")


def serve_file(field, download_name=None, inline=True):
    """Отдаёт файл после проверки прав вызывающим представлением.

    Для S3 — редирект на подписанную ссылку с ограниченным сроком действия.
    """
    name = download_name or os.path.basename(field.name)
    storage = field.storage
    if storage.__class__.__name__ == "S3Storage":
        url = storage.url(
            field.name,
            parameters={"ResponseContentDisposition": f"{'inline' if inline else 'attachment'}; filename*=UTF-8''{_quote(name)}"},
        )
        return HttpResponseRedirect(url)
    content_type = mimetypes.guess_type(name)[0] or "application/octet-stream"
    response = FileResponse(field.open("rb"), content_type=content_type, as_attachment=not inline, filename=name)
    response["X-Content-Type-Options"] = "nosniff"
    return response


def _quote(value):
    from urllib.parse import quote

    return quote(value)


def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def sha256_field(field):
    digest = hashlib.sha256()
    with field.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


# ---------- PDF ----------

@lru_cache(maxsize=1)
def pdf_fonts():
    """Регистрирует шрифт с кириллицей и возвращает (обычный, жирный)."""
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont

    regular = next((p for p in settings.PDF_FONT_CANDIDATES if os.path.exists(p)), None)
    if not regular:
        raise RuntimeError("Не найден шрифт с кириллицей для PDF: задайте PDF_FONT_PATH")
    bold = next((p for p in settings.PDF_FONT_BOLD_CANDIDATES if os.path.exists(p)), regular)
    pdfmetrics.registerFont(TTFont("DocFont", regular))
    pdfmetrics.registerFont(TTFont("DocFont-Bold", bold))
    from reportlab.lib.fonts import addMapping

    addMapping("DocFont", 0, 0, "DocFont")
    addMapping("DocFont", 1, 0, "DocFont-Bold")
    addMapping("DocFont", 0, 1, "DocFont")
    addMapping("DocFont", 1, 1, "DocFont-Bold")
    return "DocFont", "DocFont-Bold"


def pdf_styles():
    from reportlab.lib.enums import TA_CENTER
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet

    regular, bold = pdf_fonts()
    base = getSampleStyleSheet()
    return {
        "normal": ParagraphStyle("n", parent=base["Normal"], fontName=regular, fontSize=10.5, leading=14),
        "small": ParagraphStyle("s", parent=base["Normal"], fontName=regular, fontSize=8.5, leading=11),
        "bold": ParagraphStyle("b", parent=base["Normal"], fontName=bold, fontSize=10.5, leading=14),
        "title": ParagraphStyle("t", parent=base["Title"], fontName=bold, fontSize=14, leading=18, alignment=TA_CENTER),
        "center": ParagraphStyle("c", parent=base["Normal"], fontName=regular, fontSize=10.5, leading=14, alignment=TA_CENTER),
        "h2": ParagraphStyle("h2", parent=base["Heading2"], fontName=bold, fontSize=12, leading=16, spaceBefore=8),
    }


def qr_image(data, size_mm=28):
    import io

    import qrcode
    from reportlab.lib.units import mm
    from reportlab.platypus import Image

    img = qrcode.make(data, box_size=6, border=1)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return Image(buf, width=size_mm * mm, height=size_mm * mm)


# ---------- одноразовые коды (простая ЭП) ----------

def send_signing_code(user, purpose, document_title):
    if not user.email:
        raise ValidationError("У пользователя не указан e-mail — невозможно отправить код подтверждения.")
    code = OneTimeCode.issue(user, purpose)
    send_mail(
        f"Код подтверждения подписи: {code}",
        (
            f"Код для подписания документа «{document_title}»: {code}\n"
            f"Код действует {settings.OTP_TTL_MINUTES} минут. Никому его не сообщайте.\n"
            "Если вы не запрашивали подпись — сообщите администратору системы."
        ),
        settings.DEFAULT_FROM_EMAIL,
        [user.email],
    )


def check_signing_code(user, purpose, code):
    if not OneTimeCode.verify(user, purpose, code):
        raise ValidationError("Неверный или просроченный код подтверждения.")


# ---------- состав ----------

@transaction.atomic
def register_signed_order(order, number, signed_on, scan_file=None):
    """Регистрирует подписанный приказ и вводит в действие связанные изменения состава.

    Все изменения вступают в силу с даты подписания приказа.
    """
    if order.status == Order.Status.SIGNED:
        raise ValidationError("Приказ уже зарегистрирован как подписанный.")
    order.number = number
    order.signed_on = signed_on
    if scan_file is not None:
        order.scan_file = scan_file
    order.status = Order.Status.SIGNED
    order.save()
    commission = order.commission
    for change in order.changes.filter(status=CompositionChange.Status.PROJECT).select_related("user"):
        apply_change(commission, change, order)
    return order


def _current_records(commission, user, on_date):
    return commission.members_on(on_date).filter(user=user)


def apply_change(commission, change, order):
    on_date = order.signed_on
    user = change.user
    current = list(_current_records(commission, user, on_date))

    def close(records):
        for rec in records:
            rec.end_date = on_date
            rec.end_order = order
            rec.save()

    if change.action == CompositionChange.Action.ADD:
        if current:
            raise ValidationError(f"{user} уже входит в состав комиссии.")
        _ensure_single_officer(commission, change.new_role, on_date, order)
        MemberRecord.objects.create(
            commission=commission, user=user, role=change.new_role or MemberRecord.Role.MEMBER,
            start_date=on_date, start_order=order,
        )
    elif change.action == CompositionChange.Action.REMOVE:
        if not current:
            raise ValidationError(f"{user} не входит в состав комиссии.")
        close(current)
    elif change.action in (CompositionChange.Action.CHANGE_ROLE, CompositionChange.Action.UPDATE_POSITION):
        if not current:
            raise ValidationError(f"{user} не входит в состав комиссии.")
        old = current[0]
        role = change.new_role or old.role
        position = change.new_position or old.position_text
        close(current)
        if change.action == CompositionChange.Action.CHANGE_ROLE:
            _ensure_single_officer(commission, role, on_date, order)
        MemberRecord.objects.create(
            commission=commission, user=user, role=role, position_text=position,
            city_text=old.city_text, start_date=on_date, start_order=order,
        )
    change.status = CompositionChange.Status.APPLIED
    change.applied_on = on_date
    change.order = order
    change.save()


def _ensure_single_officer(commission, role, on_date, order):
    """Председатель и секретарь — по одному: прежний выводится из этой роли тем же приказом,
    если приказ не содержит отдельного изменения по нему (тогда оно применится само)."""
    if role not in (MemberRecord.Role.CHAIRMAN, MemberRecord.Role.SECRETARY):
        return
    for rec in commission.members_on(on_date).filter(role=role):
        pending = order.changes.filter(user=rec.user, status=CompositionChange.Status.PROJECT).exists()
        if pending:
            continue
        raise ValidationError(
            f"В комиссии уже есть {rec.get_role_display().lower()} ({rec.user}). "
            "Добавьте в приказ изменение по действующему сотруднику."
        )


def composition_diff(commission, date_a, date_b):
    """Сравнение редакций состава на две даты."""
    def as_map(on_date):
        return {r.user_id: r for r in commission.members_on(on_date)}

    a, b = as_map(date_a), as_map(date_b)
    added = [b[k] for k in b.keys() - a.keys()]
    removed = [a[k] for k in a.keys() - b.keys()]
    changed = [
        (a[k], b[k]) for k in a.keys() & b.keys()
        if (a[k].role, a[k].position_text) != (b[k].role, b[k].position_text)
    ]
    same = [b[k] for k in a.keys() & b.keys() if (a[k].role, a[k].position_text) == (b[k].role, b[k].position_text)]
    key = lambda r: r.user.last_name  # noqa: E731
    return {
        "added": sorted(added, key=key),
        "removed": sorted(removed, key=key),
        "changed": sorted(changed, key=lambda p: p[1].user.last_name),
        "same": sorted(same, key=key),
    }
