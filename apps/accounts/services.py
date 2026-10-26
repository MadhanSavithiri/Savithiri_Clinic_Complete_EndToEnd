"""Business rules for staff accounts. Every change is atomic and audited. Passwords are never logged."""
import secrets

from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.db import transaction

from apps.core.audit import log_event

User = get_user_model()
_SAFE_CHARS = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789"  # no 0/O/1/l/I


class UserServiceError(Exception):
    """A rule was broken; the message is safe to show to the administrator."""


def generate_temp_password(user, length=12):
    while True:
        pw = "".join(secrets.choice(_SAFE_CHARS) for _ in range(length))
        if not (any(c.islower() for c in pw) and any(c.isupper() for c in pw) and any(c.isdigit() for c in pw)):
            continue
        try:
            validate_password(pw, user)
        except ValidationError:
            continue
        return pw


def _active_admins_locked():
    return list(User.objects.select_for_update().filter(role=User.Role.ADMIN, is_active=True))


def _is_last_active_admin(user):
    admins = _active_admins_locked()
    return user.role == User.Role.ADMIN and user.is_active and len(admins) <= 1


def _sync_admin_flags(user):
    is_admin = user.role == User.Role.ADMIN
    user.is_staff = is_admin
    user.is_superuser = is_admin


def create_staff_user(*, actor, request, username, first_name, last_name, role, phone):
    with transaction.atomic():
        user = User(username=username, first_name=first_name, last_name=last_name, role=role, phone=phone)
        _sync_admin_flags(user)
        temp = generate_temp_password(user)
        user.set_password(temp)
        user.must_change_password = True
        user.save()
        log_event(user=actor, action="user.create", obj=user, summary=f"Role: {user.get_role_display()}", request=request)
    return user, temp


def update_staff_user(*, actor, request, user, first_name, last_name, role, phone):
    with transaction.atomic():
        user = User.objects.select_for_update().get(pk=user.pk)
        if role != user.role:
            if user.pk == actor.pk:
                raise UserServiceError("You cannot change your own role.")
            if _is_last_active_admin(user):
                raise UserServiceError("At least one active administrator is required.")
        new = {"first_name": first_name, "last_name": last_name, "role": role, "phone": phone}
        changes = {f: [getattr(user, f), v] for f, v in new.items() if getattr(user, f) != v}
        if not changes:
            return user
        for f, v in new.items():
            setattr(user, f, v)
        _sync_admin_flags(user)
        user.save()
        log_event(user=actor, action="user.update", obj=user, changes=changes, request=request)
    return user


def set_user_active(*, actor, request, user, active):
    with transaction.atomic():
        user = User.objects.select_for_update().get(pk=user.pk)
        if not active:
            if user.pk == actor.pk:
                raise UserServiceError("You cannot deactivate your own account.")
            if _is_last_active_admin(user):
                raise UserServiceError("At least one active administrator is required.")
        if user.is_active == active:
            return user
        user.is_active = active
        user.save(update_fields=["is_active"])
        log_event(user=actor, action="user.activate" if active else "user.deactivate", obj=user, request=request)
    return user


def reset_password(*, actor, request, user):
    with transaction.atomic():
        user = User.objects.select_for_update().get(pk=user.pk)
        if user.pk == actor.pk:
            raise UserServiceError("Use 'Change password' for your own account.")
        temp = generate_temp_password(user)
        user.set_password(temp)
        user.must_change_password = True
        user.failed_login_count = 0
        user.locked_until = None
        user.save(update_fields=["password", "must_change_password", "failed_login_count", "locked_until"])
        log_event(user=actor, action="user.reset_password", obj=user, request=request)
    return user, temp


def unlock_user(*, actor, request, user):
    with transaction.atomic():
        user = User.objects.select_for_update().get(pk=user.pk)
        user.failed_login_count = 0
        user.locked_until = None
        user.save(update_fields=["failed_login_count", "locked_until"])
        log_event(user=actor, action="user.unlock", obj=user, request=request)
    return user
