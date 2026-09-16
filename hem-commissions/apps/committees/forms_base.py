from django import forms


class StyledFormMixin:
    """Единые виджеты: нативные поля даты/времени, пустые варианты выбора."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            widget = field.widget
            if isinstance(field, forms.DateTimeField) and not isinstance(widget, forms.HiddenInput):
                field.widget = forms.DateTimeInput(attrs={"type": "datetime-local"}, format="%Y-%m-%dT%H:%M")
                field.input_formats = ["%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M", "%d.%m.%Y %H:%M"]
            elif isinstance(field, forms.DateField) and not isinstance(widget, forms.HiddenInput):
                field.widget = forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d")
                field.input_formats = ["%Y-%m-%d", "%d.%m.%Y"]
            elif isinstance(field, forms.TimeField):
                field.widget = forms.TimeInput(attrs={"type": "time"}, format="%H:%M")
            elif isinstance(widget, forms.Textarea):
                widget.attrs.setdefault("rows", 4)


class StyledForm(StyledFormMixin, forms.Form):
    pass


class StyledModelForm(StyledFormMixin, forms.ModelForm):
    pass


class UserChoiceField(forms.ModelChoiceField):
    def label_from_instance(self, obj):
        parts = [obj.full_name or obj.username]
        if obj.position_id:
            parts.append(obj.position.name)
        if obj.city_id:
            parts.append(obj.city.name)
        return " — ".join(parts)


class UserMultipleChoiceField(forms.ModelMultipleChoiceField):
    def label_from_instance(self, obj):
        return UserChoiceField.label_from_instance(self, obj)


def active_users():
    from apps.accounts.models import User

    return User.objects.filter(is_active=True).select_related("position", "city").order_by("last_name", "first_name")
