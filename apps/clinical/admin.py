from django.contrib import admin

from apps.patients.admin import ReadOnlyAdmin

from . import models


@admin.register(models.Encounter)
class EncounterAdmin(ReadOnlyAdmin):
    list_display = ("id", "patient", "doctor", "status", "started_at")
    list_filter = ("status",)


@admin.register(models.Prescription)
class PrescriptionAdmin(ReadOnlyAdmin):
    list_display = ("number", "patient", "version", "status", "finalized_at")
    list_filter = ("status",)


for _model in (models.EncounterRevision, models.ClinicalNote, models.VitalObservation, models.Diagnosis,
               models.Allergy, models.MedicalHistoryItem, models.CurrentMedicine, models.FollowUp, models.Referral,
               models.ClinicalProfile, models.PrescriptionItem):
    admin.site.register(_model, type(f"{_model.__name__}Admin", (ReadOnlyAdmin,), {}))
