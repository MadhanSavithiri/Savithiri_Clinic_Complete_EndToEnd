from django.conf import settings
from django.shortcuts import redirect
from django.urls import reverse


class MustChangePasswordMiddleware:
    """Staff given a temporary password can do nothing except change it (or sign out)."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        user = getattr(request, "user", None)
        if user is not None and user.is_authenticated and user.must_change_password:
            allowed = {
                reverse("accounts:password_change"),
                reverse("accounts:logout"),
                reverse("health"),
            }
            static_prefix = "/" + settings.STATIC_URL.lstrip("/")
            if request.path not in allowed and not request.path.startswith(static_prefix):
                return redirect("accounts:password_change")
        return self.get_response(request)


class SessionActivityMiddleware:
    """Slides the idle timeout forward on real actions, but NOT on background live-update polls."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        user = getattr(request, "user", None)
        if user is not None and user.is_authenticated and request.META.get("HTTP_X_LIVE_POLL") != "1":
            request.session.modified = True
        return self.get_response(request)
