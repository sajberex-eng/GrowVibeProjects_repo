from django import forms
from django.core.exceptions import ValidationError
from django.utils import timezone

from apps.committees.forms_base import StyledForm, StyledModelForm, UserChoiceField
from apps.committees.services import validate_upload

from .models import Vote, VotingMaterial, VotingSession


class VotingSessionForm(StyledModelForm):
    class Meta:
        model = VotingSession
        fields = ["question", "description", "deadline", "allow_comments"]
        widgets = {"description": forms.Textarea(attrs={"rows": 6})}
        help_texts = {
            "deadline": "По умолчанию — через установленное в комиссии число рабочих дней, 18:00.",
        }

    def clean_deadline(self):
        deadline = self.cleaned_data["deadline"]
        if deadline <= timezone.now():
            raise ValidationError("Срок окончания должен быть в будущем.")
        return deadline


class CommissionSelectForm(StyledForm):
    commission = forms.ModelChoiceField(label="Комиссия", queryset=None, empty_label=None)

    def __init__(self, *args, commissions=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["commission"].queryset = commissions


class MaterialForm(StyledModelForm):
    upload = forms.FileField(label="Файл", required=False)

    class Meta:
        model = VotingMaterial
        fields = ["title", "url"]
        labels = {"url": "или ссылка"}

    def clean(self):
        data = super().clean()
        upload = data.get("upload")
        if bool(upload) == bool(data.get("url")):
            raise ValidationError("Приложите файл или укажите ссылку (что-то одно).")
        if upload:
            validate_upload(upload)
        return data

    def save(self, session, commit=True):
        material = super().save(commit=False)
        material.session = session
        upload = self.cleaned_data.get("upload")
        if upload:
            material.file = upload
            material.file_name = upload.name[:255]
        if commit:
            material.save()
        return material


class VoteForm(StyledForm):
    choice = forms.ChoiceField(label="Ваш голос", choices=Vote.Choice.choices, widget=forms.RadioSelect)
    comment = forms.CharField(label="Комментарий", required=False, widget=forms.Textarea(attrs={"rows": 3}))


class EnterVoteForm(VoteForm):
    user = UserChoiceField(label="Член комиссии", queryset=None)

    def __init__(self, *args, voters=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["user"].queryset = voters
        self.order_fields(["user", "choice", "comment"])
