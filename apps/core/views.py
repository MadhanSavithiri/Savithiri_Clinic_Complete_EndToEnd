import logging

from django.contrib.auth.decorators import login_required
from django.db import connection
from django.http import JsonResponse
from django.shortcuts import redirect, render

from apps.accounts.permissions import user_has_permission

from .workspaces import WORKSPACE_CARDS

logger = logging.getLogger(__name__)


def health(request):
    """Used by monitoring/startup scripts. Reveals no patient data and no internals."""
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()
    except Exception:
        logger.exception("Health check: database unreachable")
        return JsonResponse({"status": "error"}, status=503)
    return JsonResponse({"status": "ok"})


@login_required
def home(request):
    if request.user.role == "counter":  # the counter salesperson works from one screen
        return redirect("sales:counter")
    cards = [c for c in WORKSPACE_CARDS if user_has_permission(request.user, c["permission"])]
    return render(request, "home.html", {"cards": cards})
