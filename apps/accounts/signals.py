from datetime import timedelta

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.signals import user_logged_in, user_logged_out, user_login_failed
from django.db import transaction
from django.dispatch import receiver
from django.utils import timezone

from apps.core.audit import log_event


@receiver(user_login_failed)
def on_login_failed(sender, credentials, request=None, **kwargs):
    User = get_user_model()
    typed = (credentials or {}).get("username") or ""
    with transaction.atomic():
        user = User.objects.select_for_update().filter(username__iexact=typed).first() if typed else None
        if user is None:
            # The typed text is NOT stored: people sometimes type a password into the username box.
            log_event(user=None, action="auth.login_failed", summary="Unknown username", request=request)
            return
        if user.is_locked():
            return  # already locked: do not extend the lock or flood the audit log
        user.failed_login_count += 1
        just_locked = False
        if user.failed_login_count >= settings.LOGIN_MAX_FAILED_ATTEMPTS:
            user.locked_until = timezone.now() + timedelta(minutes=settings.LOGIN_LOCK_MINUTES)
            user.failed_login_count = 0
            just_locked = True
        user.save(update_fields=["failed_login_count", "locked_until"])
        log_event(user=user, action="auth.login_failed", obj=user, summary="Wrong password", request=request)
        if just_locked:
            log_event(
                user=user,
                action="auth.account_locked",
                obj=user,
                summary=f"Locked for {settings.LOGIN_LOCK_MINUTES} minutes after repeated failures",
                request=request,
            )


@receiver(user_logged_in)
def on_login(sender, request, user, **kwargs):
    type(user).objects.filter(pk=user.pk).update(failed_login_count=0, locked_until=None)
    log_event(user=user, action="auth.login", obj=user, request=request)


@receiver(user_logged_out)
def on_logout(sender, request, user, **kwargs):
    if user is not None:
        log_event(user=user, action="auth.logout", obj=user, request=request)
