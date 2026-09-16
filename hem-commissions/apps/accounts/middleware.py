import time

from django.conf import settings
from django.contrib.auth import logout
from django.shortcuts import redirect
from django.urls import reverse


class SessionIdleTimeoutMiddleware:
    """Завершает сессию после периода бездействия."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.user.is_authenticated:
            now = int(time.time())
            last = request.session.get("last_activity")
            if last and now - last > settings.SESSION_IDLE_TIMEOUT_MINUTES * 60:
                logout(request)
                return redirect(f"{reverse('accounts:login')}?timeout=1")
            request.session["last_activity"] = now
        return self.get_response(request)


class ForcePasswordChangeMiddleware:
    """Принудительная смена пароля при первом входе / после сброса администратором."""

    ALLOWED_PREFIXES = ("/accounts/", "/static/", "/verify/")

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        user = request.user
        if (
            user.is_authenticated
            and user.must_change_password
            and not request.path.startswith(self.ALLOWED_PREFIXES)
        ):
            return redirect("accounts:password_change")
        return self.get_response(request)
