import re
from datetime import time, timedelta

from django import forms
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import UploadedFile
from django.utils import timezone

from apps.accounts.models import City
from apps.committees.forms_base import StyledForm, StyledModelForm, UserChoiceField, active_users
from apps.committees.models import Commission, Rubric
from apps.committees.services import validate_upload

from . import schedule
from .models import (
    PATIENT_ID_RE, AgendaAcknowledgement, AgendaItem, AgendaProposal, Invitation, Material, Meeting, Transcript,
)

PATIENT_HELP = "Только ID из МИС, без ФИО/ИИН."
IIN_RE = re.compile(r"^\d{12}$")


def clean_patient_id(value):
    value = (value or "").strip()
    if not value:
        return ""
    if not PATIENT_ID_RE.match(value):
        raise ValidationError(
            "Допустим только внутренний ID пациента из МИС: латиница/кириллица, цифры и символы - / _ . без пробелов."
        )
    if IIN_RE.match(value):
        raise ValidationError("Значение похоже на ИИН. Укажите внутренний ID пациента из МИС.")
    if not any(ch.isdigit() for ch in value):
        raise ValidationError("ID пациента из МИС должен содержать цифры (не указывайте ФИО).")
    return value


class PatientIdMixin:
    """Поле patient_id показывается только комиссиям, рассматривающим клинические случаи."""

    def setup_patient_field(self, commission):
        if "patient_id" not in self.fields:
            return
        if commission is None or not commission.handles_patient_cases:
            del self.fields["patient_id"]
        else:
            self.fields["patient_id"].help_text = PATIENT_HELP
            self.fields["patient_id"].widget.attrs.update({"autocomplete": "off", "placeholder": "например, MIS-104233"})

    def clean_patient_id(self):
        return clean_patient_id(self.cleaned_data.get("patient_id"))


class MeetingForm(StyledModelForm):
    commission = forms.ModelChoiceField(Commission.objects.none(), label="Комиссия")

    class Meta:
        model = Meeting
        fields = [
            "commission", "kind", "starts_at", "duration_minutes", "format", "place", "video_link", "cities",
            "proposals_deadline",
        ]
        widgets = {"cities": forms.CheckboxSelectMultiple}

    def __init__(self, *args, commissions=None, commission=None, editing=False, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["cities"].queryset = City.objects.all()
        self.fields["proposals_deadline"].help_text = "Пусто — предложения принимаются до утверждения повестки."
        self.fields["video_link"].help_text = "Для онлайн и смешанного формата."
        if editing or commission is not None:
            del self.fields["commission"]
        else:
            self.fields["commission"].queryset = commissions if commissions is not None else Commission.objects.none()
        if editing:
            # Дата меняется только через «Перенести» — с записью в журнал переносов.
            del self.fields["starts_at"]
        if commission is not None and not self.instance.pk:
            self.initial.setdefault("cities", list(commission.cities.values_list("pk", flat=True)))

    def clean_starts_at(self):
        value = self.cleaned_data["starts_at"]
        if value and value < timezone.now() - timedelta(days=1):
            raise ValidationError("Дата заседания в прошлом.")
        return value

    def clean(self):
        data = super().clean()
        fmt = data.get("format")
        if fmt in (Meeting.Format.ONLINE, Meeting.Format.MIXED) and not data.get("video_link") and not (
            self.instance.pk and self.instance.video_link
        ):
            self.add_error("video_link", "Укажите ссылку на видеоконференцию.")
        if fmt in (Meeting.Format.OFFLINE, Meeting.Format.MIXED) and not data.get("place"):
            self.add_error("place", "Укажите место проведения.")
        deadline = data.get("proposals_deadline")
        starts_at = data.get("starts_at") or self.instance.starts_at
        if deadline and starts_at and deadline > starts_at:
            self.add_error("proposals_deadline", "Срок приёма предложений позже даты заседания.")
        return data


class RescheduleForm(StyledForm):
    new_starts_at = forms.DateTimeField(label="Новая дата и время")
    reason = forms.CharField(label="Причина переноса", max_length=300)

    def clean_new_starts_at(self):
        value = self.cleaned_data["new_starts_at"]
        if value < timezone.now():
            raise ValidationError("Новая дата должна быть в будущем.")
        return value


class CancelForm(StyledForm):
    reason = forms.CharField(label="Причина отмены", max_length=300)


class AgendaItemForm(PatientIdMixin, StyledModelForm):
    speaker = UserChoiceField(active_users(), label="Докладчик (пользователь системы)", required=False)

    class Meta:
        model = AgendaItem
        fields = ["title", "description", "rubric", "speaker", "speaker_name", "duration_minutes", "patient_id", "requires_vote"]
        widgets = {"title": forms.TextInput(attrs={"wide": True})}

    def __init__(self, *args, commission=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["speaker"].queryset = active_users()
        self.fields["rubric"].queryset = Rubric.objects.filter(commission=commission) if commission else Rubric.objects.none()
        self.fields["requires_vote"].help_text = (
            "Члены комиссии получат письмо со ссылкой и проголосуют в личном кабинете (кворум — "
            + (commission.vote_quorum_label if commission else "2/3 членов") + ")."
        )
        self.setup_patient_field(commission)

    def clean(self):
        data = super().clean()
        if data.get("speaker") and data.get("speaker_name"):
            self.add_error("speaker_name", "Укажите докладчика либо из списка, либо вручную.")
        return data


class ProposalForm(PatientIdMixin, StyledModelForm):
    class Meta:
        model = AgendaProposal
        fields = ["title", "description", "speaker_name", "patient_id", "requires_vote", "file"]
        widgets = {"title": forms.TextInput(attrs={"wide": True})}

    def __init__(self, *args, commission=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.setup_patient_field(commission)
        self.fields["file"].help_text = "Необязательно. Не прикладывайте документы с ФИО/ИИН пациентов."

    def clean_file(self):
        f = self.cleaned_data.get("file")
        if isinstance(f, UploadedFile):
            validate_upload(f)
            self.instance.file_name = f.name[:255]
        return f


class ProposalRejectForm(StyledForm):
    response = forms.CharField(label="Ответ автору (причина отклонения)", max_length=500, widget=forms.Textarea)


class AcknowledgementForm(StyledForm):
    status = forms.ChoiceField(label="Отметка", choices=AgendaAcknowledgement.Status.choices, widget=forms.RadioSelect)
    text = forms.CharField(label="Предложение по повестке", required=False, widget=forms.Textarea)

    def clean(self):
        data = super().clean()
        if data.get("status") == AgendaAcknowledgement.Status.SUGGESTION and not data.get("text", "").strip():
            self.add_error("text", "Опишите предложение.")
        return data


class MaterialFileForm(StyledModelForm):
    class Meta:
        model = Material
        fields = ["title", "agenda_item", "file"]

    def __init__(self, *args, meeting=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["agenda_item"].queryset = meeting.agenda_items.all() if meeting else AgendaItem.objects.none()
        self.fields["file"].required = True
        self.fields["title"].required = False
        self.fields["title"].help_text = "Пусто — используется имя файла."

    def clean_file(self):
        f = self.cleaned_data.get("file")
        if f:
            validate_upload(f)
        return f

    def clean(self):
        data = super().clean()
        f = data.get("file")
        if f and not data.get("title"):
            data["title"] = f.name[:300]
            self.instance.title = data["title"]
        if f:
            self.instance.file_name = f.name[:255]
        return data


class MaterialLinkForm(StyledModelForm):
    kind = forms.ChoiceField(
        label="Вид",
        choices=[(k, v) for k, v in Material.Kind.choices if k != Material.Kind.FILE],
    )

    class Meta:
        model = Material
        fields = ["kind", "title", "url", "agenda_item"]

    def __init__(self, *args, meeting=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["agenda_item"].queryset = meeting.agenda_items.all() if meeting else AgendaItem.objects.none()
        self.fields["url"].required = True
        self.fields["url"].help_text = (
            "Аудиозапись в систему не загружается — укажите ссылку на запись в хранилище."
        )


class InvitationForm(StyledModelForm):
    user = UserChoiceField(active_users(), label="Приглашённый")

    class Meta:
        model = Invitation
        fields = ["user", "valid_until", "note"]

    def __init__(self, *args, meeting=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.meeting = meeting
        members = list(meeting.composition().values_list("user_id", flat=True)) if meeting else []
        invited = list(meeting.invitations.values_list("user_id", flat=True)) if meeting else []
        self.fields["user"].queryset = active_users().exclude(pk__in=members + invited)
        self.fields["valid_until"].help_text = "После этой даты доступ к заседанию закрывается."
        if meeting and not self.initial.get("valid_until"):
            self.initial["valid_until"] = meeting.meeting_date + timedelta(days=30)

    def clean_valid_until(self):
        value = self.cleaned_data["valid_until"]
        if value < timezone.localdate():
            raise ValidationError("Дата в прошлом.")
        return value


class ScheduleForm(StyledForm):
    year = forms.IntegerField(label="Год", min_value=2020, max_value=2100)
    rule = forms.ChoiceField(label="Правило", choices=schedule.RULE_CHOICES)
    nth = forms.TypedChoiceField(label="Какой по счёту", choices=schedule.NTH_CHOICES, coerce=int, required=False)
    weekday = forms.TypedChoiceField(label="День недели", choices=schedule.WEEKDAY_CHOICES, coerce=int, required=False)
    day = forms.IntegerField(label="Число месяца", min_value=1, max_value=31, required=False,
                             help_text="Если выпадает на выходной/праздник — переносится на следующий рабочий день.")
    quarter_month = forms.TypedChoiceField(label="Месяц квартала", choices=schedule.QUARTER_MONTH_CHOICES, coerce=int,
                                           required=False)
    start_time = forms.TimeField(label="Время начала", initial=time(14, 0))
    duration_minutes = forms.IntegerField(label="Длительность, мин", min_value=10, max_value=600, initial=90)
    format = forms.ChoiceField(label="Формат", choices=Meeting.Format.choices)
    place = forms.CharField(label="Место", max_length=300, required=False)
    video_link = forms.URLField(label="Ссылка на видеоконференцию", required=False)

    def clean(self):
        data = super().clean()
        rule = data.get("rule")
        if rule in (schedule.RULE_NTH_WEEKDAY, schedule.RULE_QUARTERLY):
            if data.get("nth") in (None, ""):
                self.add_error("nth", "Выберите, какой по счёту день недели.")
            if data.get("weekday") in (None, ""):
                self.add_error("weekday", "Выберите день недели.")
            if rule == schedule.RULE_QUARTERLY and not data.get("quarter_month"):
                self.add_error("quarter_month", "Выберите месяц квартала.")
        elif rule == schedule.RULE_DAY_OF_MONTH and not data.get("day"):
            self.add_error("day", "Укажите число месяца.")
        return data

    def dates(self):
        d = self.cleaned_data
        return schedule.generate_dates(
            d["year"], d["rule"], weekday=d.get("weekday"), n=d.get("nth"), day=d.get("day"),
            quarter_month=d.get("quarter_month") or 1,
        )

    def describe(self):
        d = self.cleaned_data
        return schedule.describe_rule(d["rule"], d.get("weekday"), d.get("nth"), d.get("day"), d.get("quarter_month") or 1)


class TranscriptForm(StyledModelForm):
    class Meta:
        model = Transcript
        fields = ["audio", "text"]
        widgets = {"text": forms.Textarea(attrs={"rows": 18})}

    def __init__(self, *args, meeting=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["audio"].queryset = (
            meeting.materials.filter(kind=Material.Kind.AUDIO) if meeting else Material.objects.none()
        )
        self.fields["audio"].required = False
        self.fields["text"].required = True
        self.fields["text"].help_text = "Текст с таймкодами, например: [00:05:12] Секретарь: …. Не указывайте ФИО/ИИН пациентов."
