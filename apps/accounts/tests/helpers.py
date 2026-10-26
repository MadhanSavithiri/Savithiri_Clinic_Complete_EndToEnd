from django.contrib.auth import get_user_model

PW = "Str0ng-Test-Pass-1"


def make_user(username, role, password=PW, **extra):
    User = get_user_model()
    is_admin = role == "admin"
    return User.objects.create_user(
        username=username, password=password, role=role, first_name=username.title(),
        is_staff=is_admin, is_superuser=is_admin, **extra,
    )
