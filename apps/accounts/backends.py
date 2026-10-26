from django.contrib.auth import get_user_model
from django.contrib.auth.backends import ModelBackend


class LockoutBackend(ModelBackend):
    """Refuses sign-in (even with the right password) while the account is temporarily locked.

    Only NEW sign-ins are blocked. A person already signed in is not kicked out by someone
    else typing wrong passwords against their username.
    """

    def authenticate(self, request, username=None, password=None, **kwargs):
        User = get_user_model()
        if username is None:
            username = kwargs.get(User.USERNAME_FIELD)
        if username is None or password is None:
            return None
        try:
            user = User._default_manager.get_by_natural_key(username)
        except User.DoesNotExist:
            User().set_password(password)  # keep timing similar for unknown names
            return None
        if user.is_locked():
            return None
        return super().authenticate(request, username=username, password=password, **kwargs)
