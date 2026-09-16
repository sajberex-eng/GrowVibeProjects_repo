"""Распознавание аудиозаписей заседаний.

Аудио в систему не загружается — хранится только ссылка на запись. Сервис
транскрипции (размещённый в РК или локальный) настраивается через
TRANSCRIPTION_BACKEND / TRANSCRIPTION_URL. Если не настроен — вызывается
TranscriptionNotConfigured, а текст можно внести вручную.

Поддерживаемый бэкенд «http»: POST {TRANSCRIPTION_URL} с JSON {"url": "<ссылка>"},
ответ JSON {"text": "<текст с таймкодами>"}.
"""
import json
import urllib.request

from django.conf import settings
from django.utils import timezone


class TranscriptionNotConfigured(Exception):
    pass


class TranscriptionError(Exception):
    pass


def is_configured():
    return bool(settings.TRANSCRIPTION_BACKEND)


def transcribe(url):
    """Возвращает текст расшифровки аудиозаписи по ссылке."""
    backend = (settings.TRANSCRIPTION_BACKEND or "").strip().lower()
    if not backend:
        raise TranscriptionNotConfigured("Сервис транскрипции не настроен.")
    if backend != "http":
        raise TranscriptionError(f"Неизвестный бэкенд транскрипции: {backend}")
    if not settings.TRANSCRIPTION_URL:
        raise TranscriptionNotConfigured("Не задан адрес сервиса транскрипции (TRANSCRIPTION_URL).")
    data = json.dumps({"url": url}).encode()
    req = urllib.request.Request(
        settings.TRANSCRIPTION_URL, data=data, headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=600) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except (OSError, ValueError) as exc:
        raise TranscriptionError(f"Сервис транскрипции недоступен: {exc}") from exc
    text = payload.get("text") if isinstance(payload, dict) else None
    if not text:
        raise TranscriptionError("Сервис транскрипции вернул пустой результат.")
    return text


def run_for_material(material, user=None):
    """Создаёт транскрипт для аудиоматериала. TranscriptionNotConfigured пробрасывается."""
    from .models import Transcript

    if not is_configured():
        raise TranscriptionNotConfigured("Сервис транскрипции не настроен.")
    transcript = Transcript.objects.create(
        meeting=material.meeting, audio=material, status=Transcript.Status.PROCESSING, created_by=user
    )
    try:
        transcript.text = transcribe(material.url)
        transcript.status = Transcript.Status.DONE
        transcript.error = ""
    except TranscriptionNotConfigured:
        transcript.delete()
        raise
    except TranscriptionError as exc:
        transcript.status = Transcript.Status.FAILED
        transcript.error = f"{timezone.now():%d.%m.%Y %H:%M}: {exc}"
    transcript.save()
    return transcript
