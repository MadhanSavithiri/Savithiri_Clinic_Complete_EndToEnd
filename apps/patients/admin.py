from django.contrib import admin

from .models import Patient, PatientIdentifier, PatientMergeLog


class ReadOnlyAdmin(admin.ModelAdmin):
    """Inspect only. Changes go through the clinic screens so every change is audited."""

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(Patient)
class PatientAdmin(ReadOnlyAdmin):
    list_display = ("patient_id", "full_name", "phone", "is_active", "created_at")
    search_fields = ("patient_id", "full_name", "phone")


@admin.register(PatientIdentifier)
class PatientIdentifierAdmin(ReadOnlyAdmin):
    list_display = ("patient", "id_type", "value")


@admin.register(PatientMergeLog)
class PatientMergeLogAdmin(ReadOnlyAdmin):
    list_display = ("source", "target", "merged_by", "created_at", "reason")
