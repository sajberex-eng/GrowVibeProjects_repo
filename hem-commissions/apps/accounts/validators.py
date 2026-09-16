import re

from django.core.exceptions import ValidationError


class ComplexityValidator:
    """Пароль должен содержать строчные и прописные буквы и цифру."""

    def validate(self, password, user=None):
        checks = [
            re.search(r"[a-zа-яё]", password),
            re.search(r"[A-ZА-ЯЁ]", password),
            re.search(r"\d", password),
        ]
        if not all(checks):
            raise ValidationError(
                "Пароль должен содержать строчные и прописные буквы и хотя бы одну цифру.",
                code="password_not_complex",
            )

    def get_help_text(self):
        return "Пароль должен содержать строчные и прописные буквы и хотя бы одну цифру."
