import secrets
import string

from django import forms
from django.contrib.auth import authenticate
from django.contrib.auth.forms import AuthenticationForm

from apps.committees.forms_base import StyledModelForm

from .models import User


class LockoutAuthenticationForm(AuthenticationForm):
    """Вход с блокировкой учётной записи после серии неудачных попыток."""

    error_messages = {
        "invalid_login": "Неверный логин или пароль.",
        "inactive": "Учётная запись отключена. Обратитесь к администратору.",
        "locked": "Учётная запись временно заблокирована из-за неудачных попыток входа. "
                  "Повторите позже или обратитесь к администратору.",
    }

    def clean(self):
        username = self.cleaned_data.get("username")
        password = self.cleaned_data.get("password")
        if not username or not password:
            return self.cleaned_data
        user = User.objects.filter(username__iexact=username).first()
        if user and user.is_locked:
            raise forms.ValidationError(self.error_messages["locked"], code="locked")
        self.user_cache = authenticate(self.request, username=user.username if user else username, password=password)
        if self.user_cache is None:
            if user:
                user.register_failed_login()
                if user.is_locked:
                    raise forms.ValidationError(self.error_messages["locked"], code="locked")
            raise self.get_invalid_login_error()
        self.confirm_login_allowed(self.user_cache)
        self.user_cache.reset_failed_logins()
        return self.cleaned_data


def generate_password(length=14):
    alphabet = string.ascii_letters + string.digits
    while True:
        pwd = "".join(secrets.choice(alphabet) for _ in range(length))
        if any(c.islower() for c in pwd) and any(c.isupper() for c in pwd) and any(c.isdigit() for c in pwd):
            return pwd


class ProfileForm(StyledModelForm):
    class Meta:
        model = User
        fields = ["email", "phone", "whatsapp_opt_in"]


class UserAdminForm(StyledModelForm):
    class Meta:
        model = User
        fields = [
            "username", "last_name", "first_name", "middle_name", "position", "department", "city",
            "email", "phone", "is_staff", "is_active", "dismissed_at",
        ]
        labels = {"is_staff": "Администратор системы", "is_active": "Активен", "username": "Логин"}
        help_texts = {"username": "", "is_staff": "", "is_active": ""}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for name in ("last_name", "first_name", "email"):
            self.fields[name].required = True

    def clean(self):
        data = super().clean()
        if data.get("dismissed_at") and data.get("is_active"):
            data["is_active"] = False
        return data
