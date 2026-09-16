"""PDF протокола (reportlab) по снимку данных.

Документ строится только из snapshot (см. services.build_snapshot), поэтому
одинаковый снимок даёт одинаковый PDF (invariant=1 — без случайных ID и дат).
"""
import io
from datetime import date, datetime
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.platypus import KeepTogether, PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from apps.committees.services import pdf_fonts, pdf_styles, qr_image

TRANSCRIPTION_REMARK = (
    "Протокол сформирован с использованием автоматической транскрипции; итоговый текст выверен секретарём."
)
SIGN_METHOD = "простая ЭП, подтверждена кодом из e-mail"


def _p(text, style):
    text = escape(str(text or "")).replace("\n", "<br/>")
    return Paragraph(text, style)


def _date(value):
    if not value:
        return ""
    if isinstance(value, str):
        try:
            value = date.fromisoformat(value[:10])
        except ValueError:
            return value
    return value.strftime("%d.%m.%Y")


def _dt(value):
    if not value:
        return ""
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            return value
    return value.strftime("%d.%m.%Y %H:%M")


def _people_lines(people):
    lines = []
    for n, person in enumerate(people, 1):
        parts = [person.get("name", "")]
        extra = ", ".join(x for x in [person.get("role", ""), person.get("position", "")] if x)
        if extra:
            parts.append(f"— {extra}")
        if person.get("reason"):
            parts.append(f"({person['reason']})")
        lines.append(f"{n}. " + " ".join(parts))
    return "\n".join(lines) or "—"


def _table(rows, widths, styles, header=True):
    regular, bold = pdf_fonts()
    data = [[_p(cell, styles["bold"] if header and i == 0 else styles["small"]) for cell in row] for i, row in enumerate(rows)]
    table = Table(data, colWidths=widths, repeatRows=1 if header else 0)
    table.setStyle(TableStyle([
        ("GRID", (0, 0), (-1, -1), 0.4, colors.grey),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#f1f1f1") if header else colors.white),
        ("FONTNAME", (0, 0), (-1, -1), regular),
    ]))
    return table


def build_story(snap, styles):
    s = styles
    story = []
    story.append(_p(snap.get("organization", ""), s["center"]))
    story.append(Spacer(1, 4 * mm))
    number = snap.get("number") or "б/н"
    if snap.get("kind") == "absentee":
        title = f"ПРОТОКОЛ № {number}\nзаочного голосования членов комиссии\n«{snap['commission']['name']}»"
    else:
        title = f"ПРОТОКОЛ № {number}\nзаседания комиссии\n«{snap['commission']['name']}»"
    story.append(_p(title, s["title"]))
    story.append(Spacer(1, 3 * mm))

    head = [["Дата", _date(snap.get("date"))]]
    if snap.get("time"):
        head.append(["Время начала", snap["time"]])
    if snap.get("place"):
        head.append(["Место проведения", snap["place"]])
    if snap.get("format"):
        head.append(["Форма проведения", snap["format"]])
    if snap.get("revision", 1) > 1:
        head.append(["Редакция", str(snap["revision"])])
    story.append(_table(head, [45 * mm, 125 * mm], s, header=False))
    story.append(Spacer(1, 3 * mm))

    if snap.get("preamble"):
        story.append(_p(snap["preamble"], s["normal"]))
        story.append(Spacer(1, 2 * mm))

    att = snap.get("attendance") or {}
    if snap.get("kind") == "absentee":
        story.append(_p("Приняли участие в голосовании:", s["bold"]))
    else:
        story.append(_p("Присутствовали члены комиссии:", s["bold"]))
    story.append(_p(_people_lines(att.get("present", [])), s["normal"]))
    if att.get("absent"):
        label = "Не приняли участие в голосовании:" if snap.get("kind") == "absentee" else "Отсутствовали:"
        story.append(_p(label, s["bold"]))
        story.append(_p(_people_lines(att["absent"]), s["normal"]))
    if att.get("invited"):
        story.append(_p("Приглашённые:", s["bold"]))
        story.append(_p(_people_lines(att["invited"]), s["normal"]))
    quorum = snap.get("quorum")
    if quorum:
        state = "Кворум имеется" if quorum.get("reached") else "Кворум отсутствует"
        story.append(_p(
            f"{state}: присутствует {quorum.get('present')} из {quorum.get('total')} членов комиссии "
            f"(требуется {quorum.get('needed')}; {quorum.get('label', '')}).", s["normal"],
        ))
    story.append(Spacer(1, 3 * mm))

    items = snap.get("items", [])
    story.append(_p("ПОВЕСТКА ДНЯ:", s["h2"]))
    for item in items:
        line = f"{item['position']}. {item['title']}"
        if item.get("patient_id"):
            line += f" (ID пациента: {item['patient_id']})"
        story.append(_p(line, s["normal"]))

    for item in items:
        block = [_p(f"{item['position']}. {item['title']}", s["h2"])]
        if item.get("patient_id"):
            block.append(_p(f"ID пациента (МИС): {item['patient_id']}", s["normal"]))
        if item.get("heard"):
            block += [_p("СЛУШАЛИ:", s["bold"]), _p(item["heard"], s["normal"])]
        if item.get("discussed"):
            block += [_p("ВЫСТУПИЛИ:", s["bold"]), _p(item["discussed"], s["normal"])]
        story.append(KeepTogether(block[:3]))
        story.extend(block[3:])
        resolved = item.get("resolved") or ""
        decisions = item.get("decisions") or []
        if resolved or decisions or item.get("dissent_note"):
            story.append(_p("РЕШИЛИ:", s["bold"]))
            if resolved:
                story.append(_p(resolved, s["normal"]))
            for d in decisions:
                prefix = f"{d['number']}. " if d.get("number") else "— "
                story.append(_p(prefix + d["text"], s["normal"]))
            if item.get("dissent_note"):
                story.append(_p(item["dissent_note"], s["normal"]))
        if item.get("vote_summary"):
            story.append(_p("Результаты голосования:", s["bold"]))
            story.append(_p(item["vote_summary"], s["normal"]))

    dissents = snap.get("dissents") or []
    if dissents:
        story.append(_p("ОСОБЫЕ МНЕНИЯ ЧЛЕНОВ КОМИССИИ", s["h2"]))
        for d in dissents:
            story.append(_p(f"По вопросу {d['item_position']} «{d['item_title']}» — {d['author']}", s["bold"]))
            if d.get("text"):
                story.append(_p(d["text"], s["normal"]))
            if d.get("file_name"):
                story.append(_p(f"Приложение: {d['file_name']}", s["small"]))
            story.append(_p(
                f"Подписано простой ЭП {_dt(d.get('signed_at'))}; SHA-256: {d.get('text_hash', '')}", s["small"],
            ))

    assignments = [
        (item, d, a) for item in items for d in item.get("decisions", []) for a in d.get("assignments", [])
    ]
    if assignments:
        story.append(_p("ПОРУЧЕНИЯ", s["h2"]))
        rows = [["№", "Поручение", "Ответственный", "Срок"]]
        for n, (item, d, a) in enumerate(assignments, 1):
            resp = a.get("responsible", "")
            if a.get("co_executors"):
                resp += "\nСоисполнители: " + ", ".join(a["co_executors"])
            rows.append([str(n), a["text"], resp, _date(a.get("due_date"))])
        story.append(_table(rows, [10 * mm, 90 * mm, 45 * mm, 25 * mm], s))

    tail = []
    if snap.get("transcription_used"):
        tail.append(Spacer(1, 3 * mm))
        tail.append(_p(TRANSCRIPTION_REMARK, s["small"]))

    tail.append(Spacer(1, 10 * mm))
    officers = snap.get("officers") or {}
    sign_rows = [
        ["Председатель комиссии", "_______________", officers.get("chairman", "")],
        ["Секретарь комиссии", "_______________", officers.get("secretary", "")],
    ]
    table = Table([[_p(c, s["normal"]) for c in row] for row in sign_rows], colWidths=[60 * mm, 50 * mm, 60 * mm])
    table.setStyle(TableStyle([("TOPPADDING", (0, 0), (-1, -1), 8)]))
    tail.append(table)
    story.append(KeepTogether(tail))
    return story


def build_stamp(snap, stamp, styles):
    s = styles
    story = [PageBreak(), _p("ЛИСТ ПОДПИСАНИЯ ЭЛЕКТРОННОГО ДОКУМЕНТА", s["title"]), Spacer(1, 4 * mm)]
    story.append(_p(
        f"Протокол № {snap.get('number') or 'б/н'} от {_date(snap.get('date'))}, {snap['commission']['name']}",
        s["normal"],
    ))
    story.append(_p(f"Идентификатор документа: {snap.get('uid', '')}", s["normal"]))
    story.append(_p(f"SHA-256 подписанного содержания: {stamp.get('frozen_hash', '')}", s["normal"]))
    story.append(Spacer(1, 3 * mm))
    rows = [["ФИО", "Роль", "Должность", "Дата и время", "Способ"]]
    for sig in stamp.get("signatures", []):
        rows.append([sig["full_name"], sig["role"], sig.get("position", ""), _dt(sig["signed_at"]), SIGN_METHOD])
    story.append(_table(rows, [42 * mm, 26 * mm, 40 * mm, 28 * mm, 34 * mm], s))
    story.append(Spacer(1, 5 * mm))
    qr = qr_image(stamp.get("verify_url", ""), size_mm=32)
    info = _p(
        "Проверить подлинность документа и сравнить хэш файла можно по QR-коду или ссылке:\n"
        + stamp.get("verify_url", ""),
        s["normal"],
    )
    table = Table([[qr, info]], colWidths=[38 * mm, 132 * mm])
    table.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "MIDDLE")]))
    story.append(table)
    return story


def render_protocol(snap, stamp=None):
    """PDF по снимку. stamp — данные листа подписания (для итогового документа)."""
    styles = pdf_styles()
    buf = io.BytesIO()
    number = snap.get("number") or "б/н"
    doc = SimpleDocTemplate(
        buf, pagesize=A4, leftMargin=20 * mm, rightMargin=15 * mm, topMargin=15 * mm, bottomMargin=18 * mm,
        title=f"Протокол № {number}", author=snap.get("organization", ""), subject=snap.get("uid", ""),
        creator="ИС «Комиссии»", invariant=1,
    )
    story = build_story(snap, styles)
    if stamp:
        story += build_stamp(snap, stamp, styles)
    regular, _ = pdf_fonts()
    footer = f"Протокол № {number} от {_date(snap.get('date'))} · {snap.get('uid', '')}"
    if stamp is None and snap.get("draft"):
        footer = "ПРОЕКТ · " + footer

    def on_page(canvas, doc_):
        canvas.saveState()
        canvas.setFont(regular, 7.5)
        canvas.drawString(20 * mm, 10 * mm, footer)
        canvas.drawRightString(A4[0] - 15 * mm, 10 * mm, f"стр. {doc_.page}")
        canvas.restoreState()

    doc.build(story, onFirstPage=on_page, onLaterPages=on_page)
    return buf.getvalue()
