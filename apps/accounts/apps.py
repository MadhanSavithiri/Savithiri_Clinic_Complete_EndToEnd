from django.apps import AppConfig


class AccountsConfig(AppConfig):
    name = "apps.accounts"
    label = "accounts"
    verbose_name = "User accounts"

    def ready(self):
        from . import signals  # noqa: F401  (connects login/logout handlers)
