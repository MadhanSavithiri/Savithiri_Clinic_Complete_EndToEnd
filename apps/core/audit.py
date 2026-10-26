from .models import AuditEvent


def _client_ip(request):
    if request is None:
        return None
    # REMOTE_ADDR only: X-Forwarded-For is client-controlled and not trusted on this LAN.
    return request.META.get("REMOTE_ADDR") or None


def log_event(*, user=None, action, obj=None, summary="", changes=None, request=None):
    """Write one audit record. Call inside the same transaction as the change it describes."""
    if user is None and request is not None and getattr(request, "user", None) is not None:
        if request.user.is_authenticated:
            user = request.user
    return AuditEvent.objects.create(
        user=user,
        username=user.get_username() if user else "",
        action=action,
        object_type=obj._meta.label if obj is not None else "",
        object_id=str(obj.pk) if obj is not None and obj.pk is not None else "",
        object_repr=str(obj)[:255] if obj is not None else "",
        summary=summary,
        changes=changes or {},
        ip_address=_client_ip(request),
    )
