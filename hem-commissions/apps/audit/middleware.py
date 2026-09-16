from .context import clear_context, set_context


def client_ip(request):
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR")


class AuditContextMiddleware:
    """Запоминает пользователя и IP текущего запроса для журнала аудита."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        user = request.user if getattr(request, "user", None) and request.user.is_authenticated else None
        set_context(user, client_ip(request))
        try:
            return self.get_response(request)
        finally:
            clear_context()
