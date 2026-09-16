def notifications(request):
    user = getattr(request, "user", None)
    if not user or not user.is_authenticated:
        return {}
    return {"unread_notifications": user.notifications.filter(read_at__isnull=True).count()}
