from django import forms
from django.core.exceptions import ValidationError
from django.utils import timezone

from apps.committees.forms_base import StyledForm

from .services import PERIOD_CHOICES, default_period, period_bounds


class ReportForm(StyledForm):
    commission = forms.ModelChoiceField(label="Комиссия", queryset=None, required=False, empty_label="Все доступные")
    year = forms.TypedChoiceField(label="Год", coerce=int, choices=())
    period = forms.ChoiceField(label="Период", choices=PERIOD_CHOICES)
    date_from = forms.DateField(label="С", required=False)
    date_to = forms.DateField(label="По", required=False)

    def __init__(self, data=None, commissions=None, **kwargs):
        today = timezone.localdate()
        preset, year = default_period(today)
        data = data.copy() if data is not None else None
        if data is not None:
            data.setdefault("period", preset)
            data.setdefault("year", str(year))
        super().__init__(data, **kwargs)
        self.fields["commission"].queryset = commissions
        years = list(range(today.year + 1, 2019, -1))
        self.fields["year"].choices = [(y, y) for y in years]
        self.initial.setdefault("year", year)
        self.initial.setdefault("period", preset)

    def clean(self):
        data = super().clean()
        if data.get("period") == "custom":
            if not data.get("date_from") or not data.get("date_to"):
                raise ValidationError("Для произвольного периода укажите даты «с» и «по».")
            if data["date_from"] > data["date_to"]:
                raise ValidationError("Дата «с» позже даты «по».")
        elif data.get("period") and data.get("year"):
            data["date_from"], data["date_to"] = period_bounds(data["period"], data["year"])
        return data


class SignCodeForm(StyledForm):
    code = forms.CharField(label="Код из письма", max_length=6, min_length=6,
                           widget=forms.TextInput(attrs={"inputmode": "numeric", "autocomplete": "one-time-code"}))
