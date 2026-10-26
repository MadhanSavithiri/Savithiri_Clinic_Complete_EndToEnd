from django.apps import AppConfig


class ClinicalConfig(AppConfig):
    name = "apps.clinical"
    label = "clinical"
    verbose_name = "Clinical records"

    def ready(self):
        from apps.patients.signals import patient_merged

        from .handlers import on_patient_merged

        patient_merged.connect(on_patient_merged, dispatch_uid="clinical_on_patient_merged")
