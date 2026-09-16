"""Публичная проверка подлинности протокола (без входа в систему): reverse("verify", args=[uid])."""
from django.urls import path

from . import views

urlpatterns = [
    path("<uuid:uid>/", views.verify, name="verify"),
]
