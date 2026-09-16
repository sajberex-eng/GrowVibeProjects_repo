from django import forms
from django.core.exceptions import ValidationError

from apps.committees.forms_base import StyledForm, StyledModelForm, UserChoiceField, UserMultipleChoiceField, active_users
from apps.committees.services import validate_upload
from apps.meetings.models import PATIENT_ID_RE

from .models import Assignment, Decision, Protocol, ProtocolApproval, ProtocolItem


class ProtocolHeaderForm(StyledModelForm):
    class Meta:
        model = Protocol
        fields = ["number", "protocol_date", "place", "format_text", "preamble"]
        widgets = {"preamble": forms.Textarea(attrs={"rows": 3})}

    def clean_number(self):
        number = self.cleaned_data["number"].strip()
        if number and Protocol.objects.filter(commission=self.instance.commission, number=number).exclude(pk=self.instance.pk).exists():
            raise ValidationError("Протокол с таким номером в комиссии уже существует.")
        return number


class ProtocolItemForm(StyledModelForm):
    class Meta:
        model = ProtocolItem
        fields = ["title", "patient_id", "heard", "discussed", "resolved", "vote_summary"]
        widgets = {
            "title": forms.TextInput(attrs={"wide": True}),
            "vote_summary": forms.TextInput(attrs={"wide": True}),
            "heard": forms.Textarea(attrs={"rows": 5}),
            "discussed": forms.Textarea(attrs={"rows": 10}),
            "resolved": forms.Textarea(attrs={"rows": 4}),
        }
        help_texts = {
            "patient_id": "Только внутренний ID пациента из МИС — без ФИО, ИИН и даты рождения.",
            "resolved": "Общая формулировка. Отдельные решения с поручениями добавляются ниже на странице протокола.",
        }

    def __init__(self, *args, commission=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.commission = commission
        if commission is not None and not commission.handles_patient_cases:
            self.fields.pop("patient_id")

    def clean_patient_id(self):
        value = self.cleaned_data.get("patient_id", "").strip()
        if value and not PATIENT_ID_RE.match(value):
            raise ValidationError("ID пациента: только буквы, цифры и символы - / _ . (до 40 знаков).")
        return value


class DecisionForm(StyledModelForm):
    class Meta:
        model = Decision
        fields = ["number", "text"]
        widgets = {"text": forms.Textarea(attrs={"rows": 3})}


class AssignmentForm(StyledModelForm):
    responsible = UserChoiceField(label="Ответственный", queryset=active_users(), required=False)
    co_executors = UserMultipleChoiceField(label="Соисполнители", queryset=active_users(), required=False)

    class Meta:
        model = Assignment
        fields = ["text", "responsible", "responsible_name", "co_executors", "due_date"]
        widgets = {"text": forms.Textarea(attrs={"rows": 3})}
        help_texts = {"responsible_name": "Заполните, если ответственный не является пользователем системы."}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["responsible"].queryset = active_users()
        self.fields["co_executors"].queryset = active_users()
        self.fields["co_executors"].widget.attrs["size"] = 6

    def clean(self):
        data = super().clean()
        if not data.get("responsible") and not (data.get("responsible_name") or "").strip():
            raise ValidationError("Укажите ответственного (пользователя системы или ФИО).")
        return data


class ConfirmPatientIdsForm(StyledForm):
    confirmed = forms.BooleanField(
        label="Обезличивание подтверждено: в тексте нет ФИО, ИИН и дат рождения пациентов, используются только ID из МИС",
        required=False,
    )


class ApprovalResponseForm(StyledForm):
    status = forms.ChoiceField(
        label="Ответ",
        choices=[(ProtocolApproval.Status.AGREED, "Согласен"), (ProtocolApproval.Status.REMARKS, "Есть замечания")],
        widget=forms.RadioSelect,
    )
    item = forms.ModelChoiceField(label="К вопросу (необязательно)", queryset=ProtocolItem.objects.none(), required=False)
    remarks = forms.CharField(label="Замечания", widget=forms.Textarea(attrs={"rows": 3}), required=False)

    def __init__(self, *args, protocol=None, **kwargs):
        super().__init__(*args, **kwargs)
        if protocol is not None:
            self.fields["item"].queryset = protocol.items.all()

    def clean(self):
        data = super().clean()
        if data.get("status") == ProtocolApproval.Status.REMARKS and not (data.get("remarks") or "").strip():
            self.add_error("remarks", "Укажите текст замечаний.")
        return data


class ReasonForm(StyledForm):
    reason = forms.CharField(label="Причина", widget=forms.Textarea(attrs={"rows": 3}), max_length=2000)


class CodeForm(StyledForm):
    code = forms.CharField(label="Код из письма", max_length=6, min_length=6, widget=forms.TextInput(attrs={"autocomplete": "one-time-code", "inputmode": "numeric"}))


def _validate_file(f):
    if f:
        validate_upload(f)
    return f


class DissentForm(StyledForm):
    item = forms.ModelChoiceField(label="Вопрос протокола", queryset=ProtocolItem.objects.none())
    text = forms.CharField(label="Текст особого мнения", widget=forms.Textarea(attrs={"rows": 5}), required=False)
    file = forms.FileField(label="Файл (необязательно)", required=False)
    code = forms.CharField(label="Код подтверждения из письма", max_length=6, min_length=6, widget=forms.TextInput(attrs={"autocomplete": "one-time-code", "inputmode": "numeric"}))

    def __init__(self, *args, protocol=None, **kwargs):
        super().__init__(*args, **kwargs)
        if protocol is not None:
            self.fields["item"].queryset = protocol.items.all()

    def clean_file(self):
        return _validate_file(self.cleaned_data.get("file"))

    def clean(self):
        data = super().clean()
        if not (data.get("text") or "").strip() and not data.get("file"):
            raise ValidationError("Укажите текст особого мнения или приложите файл.")
        return data


class ReportDoneForm(StyledForm):
    text = forms.CharField(label="Что сделано", widget=forms.Textarea(attrs={"rows": 3}), required=False)
    completed_on = forms.DateField(label="Дата исполнения", required=False)
    file = forms.FileField(label="Подтверждающий файл", required=False)

    def clean_file(self):
        return _validate_file(self.cleaned_data.get("file"))

    def clean(self):
        data = super().clean()
        if not (data.get("text") or "").strip() and not data.get("file"):
            raise ValidationError("Опишите результат исполнения или приложите файл.")
        return data


class CommentForm(StyledForm):
    text = forms.CharField(label="Комментарий", widget=forms.Textarea(attrs={"rows": 3}), required=False)
    file = forms.FileField(label="Файл", required=False)

    def clean_file(self):
        return _validate_file(self.cleaned_data.get("file"))


class VerifyUploadForm(forms.Form):
    file = forms.FileField(label="PDF-файл для проверки")
