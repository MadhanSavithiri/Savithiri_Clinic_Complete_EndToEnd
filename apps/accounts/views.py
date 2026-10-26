from django.contrib import messages
from django.contrib.auth import get_user_model, update_session_auth_hash
from django.contrib.auth.decorators import login_required
from django.contrib.auth.views import LoginView, LogoutView
from django.db import transaction
from django.db.models import Q
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods, require_POST

from apps.core.audit import log_event
from apps.core.models import AuditEvent

from . import services
from .decorators import permission_required
from .forms import LoginForm, StaffCreateForm, StaffEditForm, StaffPasswordChangeForm

User = get_user_model()
MANAGE = "accounts.manage_users"


class StaffLoginView(LoginView):
    template_name = "accounts/login.html"
    authentication_form = LoginForm
    redirect_authenticated_user = True


class StaffLogoutView(LogoutView):
    """POST only (Django default): a stray link can't sign someone out."""


@login_required
@never_cache
def password_change(request):
    form = StaffPasswordChangeForm(request.user, request.POST or None)
    if request.method == "POST" and form.is_valid():
        with transaction.atomic():
            user = form.save()
            user.must_change_password = False
            user.save(update_fields=["must_change_password"])
            log_event(user=user, action="auth.password_changed", obj=user, request=request)
        update_session_auth_hash(request, user)  # keep this session signed in
        messages.success(request, "Your password has been changed.")
        return redirect("home")
    return render(request, "accounts/password_change.html", {"form": form})


@permission_required(MANAGE)
def user_list(request):
    q = request.GET.get("q", "").strip()
    role = request.GET.get("role", "")
    users = User.objects.all().order_by("username")
    if q:
        users = users.filter(Q(username__icontains=q) | Q(first_name__icontains=q) | Q(last_name__icontains=q))
    if role in User.Role.values:
        users = users.filter(role=role)
    return render(request, "accounts/user_list.html", {"users": users, "q": q, "role": role, "roles": User.Role.choices})


@never_cache
@permission_required(MANAGE)
def user_create(request):
    form = StaffCreateForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        user, temp = services.create_staff_user(actor=request.user, request=request, **form.cleaned_data)
        return render(request, "accounts/temp_password.html", {"target": user, "temp_password": temp, "created": True})
    return render(request, "accounts/user_form.html", {"form": form, "creating": True})


@permission_required(MANAGE)
def user_detail(request, pk):
    target = get_object_or_404(User, pk=pk)
    events = AuditEvent.objects.filter(object_type="accounts.User", object_id=str(target.pk))[:15]
    return render(request, "accounts/user_detail.html", {"target": target, "events": events})


@permission_required(MANAGE)
def user_edit(request, pk):
    target = get_object_or_404(User, pk=pk)
    form = StaffEditForm(request.POST or None, instance=target, actor=request.user)
    if request.method == "POST" and form.is_valid():
        try:
            services.update_staff_user(actor=request.user, request=request, user=target, **form.cleaned_data)
        except services.UserServiceError as exc:
            messages.error(request, str(exc))
        else:
            messages.success(request, "Staff details saved.")
            return redirect("accounts:user_detail", pk=pk)
    return render(request, "accounts/user_form.html", {"form": form, "target": target, "creating": False})


@never_cache
@permission_required(MANAGE)
@require_http_methods(["GET", "POST"])
def user_reset_password(request, pk):
    target = get_object_or_404(User, pk=pk)
    if request.method == "POST":
        try:
            user, temp = services.reset_password(actor=request.user, request=request, user=target)
        except services.UserServiceError as exc:
            messages.error(request, str(exc))
            return redirect("accounts:user_detail", pk=pk)
        return render(request, "accounts/temp_password.html", {"target": user, "temp_password": temp, "created": False})
    return render(request, "accounts/user_confirm.html", {
        "target": target,
        "title": f"Reset password for {target.username}?",
        "body": "A new temporary password will be created. Any current sign-in of this person ends, "
                "and they must choose their own password the next time they sign in.",
        "button": "Reset password",
    })


@permission_required(MANAGE)
@require_http_methods(["GET", "POST"])
def user_toggle_active(request, pk):
    target = get_object_or_404(User, pk=pk)
    activate = not target.is_active
    if request.method == "POST":
        try:
            services.set_user_active(actor=request.user, request=request, user=target, active=activate)
        except services.UserServiceError as exc:
            messages.error(request, str(exc))
        else:
            messages.success(request, f"{target.username} is now {'active' if activate else 'deactivated'}.")
        return redirect("accounts:user_detail", pk=pk)
    return render(request, "accounts/user_confirm.html", {
        "target": target,
        "title": f"{'Activate' if activate else 'Deactivate'} {target.username}?",
        "body": ("This person will be able to sign in again." if activate else
                 "This person is signed out and cannot sign in. Their history is kept."),
        "button": "Activate" if activate else "Deactivate",
    })


@permission_required(MANAGE)
@require_POST
def user_unlock(request, pk):
    target = get_object_or_404(User, pk=pk)
    services.unlock_user(actor=request.user, request=request, user=target)
    messages.success(request, f"{target.username} has been unlocked.")
    return redirect("accounts:user_detail", pk=pk)
