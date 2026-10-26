from apps.core.audit import log_event

from .models import Allergy, ClinicalProfile, RecordStatus


def on_patient_merged(sender, target, source, moved, actor=None, **kwargs):
    """If allergies moved onto a record that said 'no known allergies', that statement is no longer true."""
    profile = ClinicalProfile.objects.filter(patient=target, no_known_allergies=True).first()
    if profile and Allergy.objects.filter(patient=target, status=RecordStatus.ACTIVE).exists():
        profile.no_known_allergies = False
        profile.save(update_fields=["no_known_allergies", "updated_at"])
        log_event(user=actor, action="clinical.nkda_cleared_by_merge", obj=target,
                  summary="Allergies were merged in; 'no known allergies' removed")
