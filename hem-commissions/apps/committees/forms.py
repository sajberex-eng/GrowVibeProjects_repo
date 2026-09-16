"""Формы приложения «Комиссии»."""
from django import forms
from django.core.exceptions import ValidationError
from django.utils import timezone

from apps.accounts.models import City

from .forms_base import StyledForm, StyledModelForm, UserChoiceField, active_users
from .models import (
    Commission, CommissionAccess, CompositionChange, LegalAct, MemberRecord, Regulation, Rubric,
)
from .services import validate_upload

SETTINGS_FIELDS = [
    "meeting_quorum_numerator", "meeting_quorum_denominator", "meeting_quorum_strict",
    "vote_quorum_numerator", "vote_quorum_denominator",
    "approval_days", "vote_days", "open_voting", "allow_vote_change", "whatsapp_enabled",
]


class _SettingsValidationMixin:
    def clean(self):
        data = super().clean()
        for prefix, label in (("meeting_quorum", "заседания"), ("vote_quorum", "голосования")):
            num = data.get(f"{prefix}_numerator")
            den = data.get(f"{prefix}_denominator")
            if num is None or den is None:
                continue
            if num < 1 or den < 1:
                raise ValidationError(f"Доля кворума {label} должна быть положительной.")
            if num > den:
                raise ValidationError(f"Доля кворума {label} не может быть больше единицы.")
        for name in ("approval_days", "vote_days"):
            if data.get(name) == 0:
                self.add_error(name, "Срок должен быть не менее 1 рабочего дня.")
        return data


class CommissionForm(_SettingsValidationMixin, StyledModelForm):
    cities = forms.ModelMultipleChoiceField(
        label="Площадки", queryset=City.objects.all(), required=False, widget=forms.CheckboxSelectMultiple,
    )

    class Meta:
        model = Commission
        fields = [
            "name", "short_name", "kind", "description", "established_on", "cities",
            "is_active", "handles_patient_cases", *SETTINGS_FIELDS,
        ]
        widgets = {"name": forms.TextInput(attrs={"wide": True})}


class CommissionSettingsForm(_SettingsValidationMixin, StyledModelForm):
    class Meta:
        model = Commission
        fields = SETTINGS_FIELDS


class CompositionChangeForm(StyledModelForm):
    user = UserChoiceField(label="Сотрудник", queryset=active_users())

    class Meta:
        model = CompositionChange
        fields = ["action", "user", "new_role", "new_position", "basis"]
        labels = {"new_role": "Роль (для ввода в состав / смены роли)"}

    def __init__(self, *args, commission=None, **kwargs):
        self.commission = commission
        super().__init__(*args, **kwargs)
        self.fields["basis"].widget.attrs["placeholder"] = "служебная записка, заявление, увольнение…"

    def clean(self):
        data = super().clean()
        action, user = data.get("action"), data.get("user")
        if not action or not user or self.commission is None:
            return data
        today = timezone.localdate()
        is_member = self.commission.members_on(today).filter(user=user).exists()
        A = CompositionChange.Action
        if action == A.ADD:
            if is_member:
                raise ValidationError(f"{user} уже входит в состав комиссии.")
            if not data.get("new_role"):
                data["new_role"] = MemberRecord.Role.MEMBER
        elif not is_member:
            raise ValidationError(f"{user} не входит в действующий состав комиссии.")
        if action == A.CHANGE_ROLE:
            if not data.get("new_role"):
                self.add_error("new_role", "Укажите новую роль.")
            elif self.commission.members_on(today).filter(user=user, role=data["new_role"]).exists():
                self.add_error("new_role", "Сотрудник уже состоит в комиссии в этой роли.")
        if action == A.UPDATE_POSITION and not data.get("new_position"):
            self.add_error("new_position", "Укажите новую должность.")
        pending = CompositionChange.objects.filter(
            commission=self.commission, user=user, status=CompositionChange.Status.PROJECT
        )
        if pending.exists():
            raise ValidationError(f"По сотруднику {user} уже есть изменение в проекте состава.")
        return data


class OrderDraftForm(StyledForm):
    title = forms.CharField(label="Заголовок приказа", max_length=300, initial="О составе комиссии")
    preamble = forms.CharField(
        label="Преамбула", required=False, widget=forms.Textarea(attrs={"rows": 3}),
        help_text="Например: «В целях организации работы … ПРИКАЗЫВАЮ:». Пусто — стандартный текст.",
    )


class OrderRegisterForm(StyledForm):
    number = forms.CharField(label="Номер приказа", max_length=50)
    signed_on = forms.DateField(label="Дата подписания", help_text="Изменения состава вступают в силу с этой даты.")
    scan_file = forms.FileField(label="Скан подписанного приказа (PDF)")

    def clean_scan_file(self):
        f = self.cleaned_data["scan_file"]
        validate_upload(f, allowed={"pdf"})
        return f

    def clean_signed_on(self):
        value = self.cleaned_data["signed_on"]
        if value > timezone.localdate():
            raise ValidationError("Дата подписания не может быть в будущем.")
        return value


class RegulationForm(StyledModelForm):
    class Meta:
        model = Regulation
        fields = ["title", "approved_on", "order_number", "order_date", "review_on", "file"]

    def __init__(self, *args, commission=None, **kwargs):
        self.commission = commission
        super().__init__(*args, **kwargs)

    def clean_file(self):
        f = self.cleaned_data["file"]
        validate_upload(f, allowed={"pdf", "docx", "doc"})
        return f

    def clean(self):
        data = super().clean()
        approved = data.get("approved_on")
        if approved and self.commission is not None:
            current = self.commission.regulations.filter(expired_on__isnull=True).order_by("-version").first()
            if current and approved <= current.approved_on:
                self.add_error("approved_on", (
                    f"Дата утверждения должна быть позже даты действующей редакции "
                    f"({current.approved_on:%d.%m.%Y})."
                ))
        review = data.get("review_on")
        if approved and review and review <= approved:
            self.add_error("review_on", "Дата пересмотра должна быть позже даты утверждения.")
        return data


class ExpireForm(StyledForm):
    expired_on = forms.DateField(label="Утратила силу с")


class LegalActForm(StyledModelForm):
    class Meta:
        model = LegalAct
        fields = ["title", "number", "act_date", "authority", "url", "note", "sort"]
        widgets = {"title": forms.TextInput(attrs={"wide": True}), "url": forms.URLInput(attrs={"wide": True})}

    def clean_url(self):
        url = self.cleaned_data.get("url", "").strip()
        if url and not url.startswith("https://"):
            raise ValidationError("Ссылка должна начинаться с https://")
        return url


class RubricForm(StyledModelForm):
    class Meta:
        model = Rubric
        fields = ["name", "sort"]

    def __init__(self, *args, commission=None, **kwargs):
        self.commission = commission
        super().__init__(*args, **kwargs)

    def clean_name(self):
        name = self.cleaned_data["name"].strip()
        # Сравнение в Python: SQLite не приводит регистр кириллицы.
        qs = Rubric.objects.filter(commission=self.commission)
        if self.instance.pk:
            qs = qs.exclude(pk=self.instance.pk)
        if any(n.casefold() == name.casefold() for n in qs.values_list("name", flat=True)):
            raise ValidationError("Такая рубрика уже есть.")
        return name


class ObserverForm(StyledForm):
    user = UserChoiceField(label="Руководитель", queryset=active_users())

    def __init__(self, *args, commission=None, **kwargs):
        self.commission = commission
        super().__init__(*args, **kwargs)

    def clean_user(self):
        user = self.cleaned_data["user"]
        if CommissionAccess.objects.filter(commission=self.commission, user=user).exists():
            raise ValidationError("Доступ уже предоставлен.")
        return user
