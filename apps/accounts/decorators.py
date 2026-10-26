from functools import wraps

from django.conf import settings
from django.contrib.auth.views import redirect_to_login
from django.core.exceptions import ImproperlyConfigured, PermissionDenied
from django.http import HttpResponse

from apps.core.audit import log_event

from .permissions import ALL_PERMISSIONS, user_has_permission


def permission_required(code):
    """Server-side check for a view. Anonymous -> login page; signed in without permission -> 403 (and audited)."""
    if code not in ALL_PERMISSIONS:
        raise ImproperlyConfigured(f"Unknown permission code: {code!r}")

    def decorator(view):
        @wraps(view)
        def wrapped(request, *args, **kwargs):
            if not request.user.is_authenticated:
                return redirect_to_login(request.get_full_path(), settings.LOGIN_URL)
            if not user_has_permission(request.user, code):
                log_event(
                    user=request.user,
                    action="auth.permission_denied",
                    summary=f"{code} on {request.path}",
                    request=request,
                )
                raise PermissionDenied
            return view(request, *args, **kwargs)

        return wrapped

    return decorator


def live_permission_required(code):
    """For background refresh endpoints: plain 401/403 (never a login page) so the browser script can react."""
    if code not in ALL_PERMISSIONS:
        raise ImproperlyConfigured(f"Unknown permission code: {code!r}")

    def decorator(view):
        @wraps(view)
        def wrapped(request, *args, **kwargs):
            if not request.user.is_authenticated:
                return HttpResponse(status=401)
            if not user_has_permission(request.user, code):
                return HttpResponse(status=403)
            return view(request, *args, **kwargs)

        return wrapped

    return decorator
