from django.apps import AppConfig
from django.db.models.signals import post_migrate


class CoreConfig(AppConfig):
    name = "apps.core"
    label = "core"
    verbose_name = "Clinic core"

    def ready(self):
        from .db_guards import install_audit_guard

        post_migrate.connect(install_audit_guard, sender=self, dispatch_uid="core_install_audit_guard")
