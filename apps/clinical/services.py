"""Clinical business rules. Every change is atomic and audited. Audit entries never contain clinical text,
only IDs, action names and the NAMES of changed fields."""
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.utils import timezone

from apps.accounts.permissions import user_has_permission
from apps.core.audit import log_event
from apps.core.models import DocType, DoctorProfile, Setting
from apps.core.sequences import next_number
from apps.patients.normalize import normalize_name
from apps.queueing import services as queue_services
from apps.queueing.models import QueueToken

from .models import (Allergy, ClinicalNote, ClinicalProfile, CurrentMedicine, Diagnosis, Encounter,
                     EncounterRevision, FollowUp, MedicalHistoryItem, Prescription, PrescriptionItem,
                     RecordStatus, Referral, VitalObservation)

RX = Prescription.Status


class ClinicalError(Exception):
    """A rule was broken; the message is safe to show to the doctor."""


def _require(actor, code):
    if not user_has_permission(actor, code):
        raise PermissionDenied


def _editable(enc):
    if enc.status != Encounter.Status.IN_PROGRESS:
        raise ClinicalError("This consultation is completed. Add an addendum, or reopen it (with a reason) to change it.")


def _lock_encounter(encounter_id):
    return Encounter.objects.select_for_update(of=("self",)).select_related("patient").get(pk=encounter_id)


def _need_reason(reason):
    reason = (reason or "").strip()
    if not reason:
        raise ClinicalError("Enter a reason.")
    return reason


# ---------------------------------------------------------------- patient-level records
def get_profile(patient):
    return ClinicalProfile.objects.get_or_create(patient=patient)[0]


def allergy_status(patient):
    """('ALLERGIES', [..]) | ('NKDA', []) | ('UNKNOWN', [])"""
    active = list(Allergy.objects.filter(patient=patient, status=RecordStatus.ACTIVE))
    if active:
        return "ALLERGIES", active
    profile = ClinicalProfile.objects.filter(patient=patient).first()
    return ("NKDA", []) if profile and profile.no_known_allergies else ("UNKNOWN", [])


def _check_patient(patient):
    if not patient.is_active:
        raise ClinicalError("This record was merged into another patient.")


def add_allergy(*, actor, request, patient, allergen, reaction="", severity=Allergy.Severity.UNKNOWN):
    _require(actor, "clinical.write_notes")
    _check_patient(patient)
    allergen = " ".join(allergen.split())
    with transaction.atomic():
        existing = Allergy.objects.filter(patient=patient, status=RecordStatus.ACTIVE)
        if any(normalize_name(a.allergen) == normalize_name(allergen) for a in existing):
            raise ClinicalError("That allergy is already recorded.")
        allergy = Allergy.objects.create(patient=patient, allergen=allergen, reaction=reaction, severity=severity,
                                         recorded_by=actor)
        profile = get_profile(patient)
        profile.no_known_allergies = False
        profile.allergies_reviewed_at, profile.allergies_reviewed_by = timezone.now(), actor
        profile.save()
        log_event(user=actor, action="allergy.add", obj=allergy, request=request)
    return allergy


def set_no_known_allergies(*, actor, request, patient):
    _require(actor, "clinical.write_notes")
    _check_patient(patient)
    with transaction.atomic():
        if Allergy.objects.filter(patient=patient, status=RecordStatus.ACTIVE).exists():
            raise ClinicalError("Active allergies are recorded. Mark them resolved first.")
        profile = get_profile(patient)
        profile.no_known_allergies = True
        profile.allergies_reviewed_at, profile.allergies_reviewed_by = timezone.now(), actor
        profile.save()
        log_event(user=actor, action="allergy.no_known", obj=patient, request=request)
    return profile


def add_history_item(*, actor, request, patient, kind, description, since=""):
    _require(actor, "clinical.write_notes")
    _check_patient(patient)
    with transaction.atomic():
        item = MedicalHistoryItem.objects.create(patient=patient, kind=kind, description=description, since=since,
                                                 recorded_by=actor)
        log_event(user=actor, action="history.add", obj=item, request=request)
    return item


def add_current_medicine(*, actor, request, patient, name, dose_notes=""):
    _require(actor, "clinical.write_notes")
    _check_patient(patient)
    with transaction.atomic():
        med = CurrentMedicine.objects.create(patient=patient, name=name, dose_notes=dose_notes, recorded_by=actor)
        log_event(user=actor, action="current_medicine.add", obj=med, request=request)
    return med


RECORD_MODELS = {"allergy": Allergy, "history": MedicalHistoryItem, "medicine": CurrentMedicine}


def set_record_status(*, actor, request, kind, record_id, status):
    _require(actor, "clinical.write_notes")
    if status not in RecordStatus.values:
        raise ClinicalError("Unknown status.")
    model = RECORD_MODELS[kind]
    with transaction.atomic():
        record = model.objects.select_for_update().get(pk=record_id)
        old = record.status
        if old == status:
            return record
        record.status = status
        record.save(update_fields=["status", "updated_at"])
        log_event(user=actor, action=f"{kind}.status", obj=record, changes={"status": [old, status]}, request=request)
    return record


# ---------------------------------------------------------------- the visit
def start_consultation(*, actor, request, token_id):
    _require(actor, "clinical.write_notes")
    _require(actor, "queue.consult")
    with transaction.atomic():
        token = QueueToken.objects.select_for_update(of=("self",)).select_related("patient").get(pk=token_id)
        enc = Encounter.objects.filter(queue_token=token).first()
        if enc:
            return enc
        if token.status in (QueueToken.Status.COMPLETED, QueueToken.Status.CANCELLED):
            raise ClinicalError("This token is already finished or cancelled.")
        _check_patient(token.patient)
        if token.status == QueueToken.Status.WAITING:
            queue_services.change_token_status(actor=actor, request=request, token_id=token.pk, action="call")
        enc = Encounter.objects.create(patient=token.patient, queue_token=token, doctor=actor)
        FollowUp.objects.filter(patient=token.patient, status=FollowUp.Status.PENDING).update(status=FollowUp.Status.DONE)
        log_event(user=actor, action="encounter.start", obj=enc, request=request)
    return enc


def update_notes(*, actor, request, encounter_id, data):
    _require(actor, "clinical.write_notes")
    with transaction.atomic():
        enc = _lock_encounter(encounter_id)
        _editable(enc)
        changed = [f for f in Encounter.NOTE_FIELDS if f in data and getattr(enc, f) != data[f]]
        if not changed:
            return enc
        for f in changed:
            setattr(enc, f, data[f])
        enc.save()
        log_event(user=actor, action="encounter.update", obj=enc, summary="Fields changed: " + ", ".join(changed),
                  request=request)
    return enc


def record_vitals(*, actor, request, encounter_id, data):
    _require(actor, "clinical.write_notes")
    with transaction.atomic():
        enc = _lock_encounter(encounter_id)
        _editable(enc)
        vital = VitalObservation.objects.create(
            encounter=enc, recorded_by=actor,
            **{k: data.get(k) for k in ("height_cm", "weight_kg", "systolic", "diastolic", "pulse", "temperature_c",
                                        "spo2", "respiratory_rate")},
            other_observations=data.get("other_observations") or "")
        log_event(user=actor, action="vitals.record", obj=vital, request=request)
    return vital


def void_vitals(*, actor, request, vital_id, reason):
    _require(actor, "clinical.write_notes")
    reason = _need_reason(reason)
    with transaction.atomic():
        vital = VitalObservation.objects.select_for_update().get(pk=vital_id)
        _editable(_lock_encounter(vital.encounter_id))
        if vital.is_void:
            return vital
        vital.is_void, vital.void_reason, vital.voided_by = True, reason, actor
        vital.save(update_fields=["is_void", "void_reason", "voided_by", "updated_at"])
        log_event(user=actor, action="vitals.void", obj=vital, request=request)
    return vital


def add_diagnosis(*, actor, request, encounter_id, description, code="", is_primary=False):
    _require(actor, "clinical.write_notes")
    with transaction.atomic():
        enc = _lock_encounter(encounter_id)
        _editable(enc)
        if is_primary:
            Diagnosis.objects.filter(encounter=enc, is_primary=True).update(is_primary=False)
        dx = Diagnosis.objects.create(
            encounter=enc, description=description, code=code, is_primary=is_primary, recorded_by=actor,
            code_system=Setting.get("diagnosis_code_system", "") if code else "")
        log_event(user=actor, action="diagnosis.add", obj=dx, request=request)
    return dx


def void_diagnosis(*, actor, request, diagnosis_id, reason=""):
    _require(actor, "clinical.write_notes")
    with transaction.atomic():
        dx = Diagnosis.objects.select_for_update().get(pk=diagnosis_id)
        _editable(_lock_encounter(dx.encounter_id))
        dx.is_void, dx.void_reason = True, (reason or "").strip()
        dx.save(update_fields=["is_void", "void_reason", "updated_at"])
        log_event(user=actor, action="diagnosis.void", obj=dx, request=request)
    return dx


def add_note(*, actor, request, encounter_id, text):
    _require(actor, "clinical.write_notes")
    with transaction.atomic():
        enc = _lock_encounter(encounter_id)
        kind = ClinicalNote.Kind.GENERAL if enc.status == Encounter.Status.IN_PROGRESS else ClinicalNote.Kind.ADDENDUM
        note = ClinicalNote.objects.create(encounter=enc, author=actor, kind=kind, text=text)
        log_event(user=actor, action="note.add", obj=note, summary=kind, request=request)
    return note


def add_referral(*, actor, request, encounter_id, referred_to, specialty="", reason):
    _require(actor, "clinical.write_notes")
    with transaction.atomic():
        enc = _lock_encounter(encounter_id)
        ref = Referral.objects.create(patient=enc.patient, encounter=enc, referred_to=referred_to,
                                      specialty=specialty, reason=reason, created_by=actor)
        log_event(user=actor, action="referral.add", obj=ref, request=request)
    return ref


def complete_consultation(*, actor, request, encounter_id):
    _require(actor, "clinical.write_notes")
    with transaction.atomic():
        enc = _lock_encounter(encounter_id)
        _editable(enc)
        if not enc.presenting_complaints.strip():
            raise ClinicalError("Record the presenting complaints before completing.")
        if not (enc.assessment.strip() or enc.diagnoses.filter(is_void=False).exists()):
            raise ClinicalError("Record a diagnosis or an assessment before completing.")
        if enc.prescriptions.filter(status=RX.DRAFT).exists():
            raise ClinicalError("There is a draft prescription. Issue it or discard it first.")
        enc.status, enc.completed_at = Encounter.Status.COMPLETED, timezone.now()
        enc.save()
        if enc.follow_up_date:
            FollowUp.objects.update_or_create(encounter=enc, defaults={
                "patient": enc.patient, "due_date": enc.follow_up_date, "notes": enc.follow_up_notes,
                "status": FollowUp.Status.PENDING, "created_by": actor})
        else:
            FollowUp.objects.filter(encounter=enc).update(status=FollowUp.Status.CANCELLED)
        token = enc.queue_token
        if token is not None:
            token = QueueToken.objects.select_for_update(of=("self",)).get(pk=token.pk)
            if token.status == QueueToken.Status.WAITING:
                queue_services.change_token_status(actor=actor, request=request, token_id=token.pk, action="call")
                token.refresh_from_db()
            if token.status == QueueToken.Status.IN_CONSULTATION:
                queue_services.change_token_status(actor=actor, request=request, token_id=token.pk, action="complete")
        log_event(user=actor, action="encounter.complete", obj=enc, request=request)
    return enc


def _snapshot(enc):
    data = {f: getattr(enc, f) for f in Encounter.NOTE_FIELDS}
    data["follow_up_date"] = enc.follow_up_date.isoformat() if enc.follow_up_date else None
    data["completed_at"] = enc.completed_at.isoformat() if enc.completed_at else None
    data["diagnoses"] = [{"description": d.description, "code": d.code, "primary": d.is_primary}
                         for d in enc.diagnoses.filter(is_void=False)]
    return data


def reopen_consultation(*, actor, request, encounter_id, reason):
    _require(actor, "clinical.authorize_correction")
    reason = _need_reason(reason)
    with transaction.atomic():
        enc = _lock_encounter(encounter_id)
        if enc.status != Encounter.Status.COMPLETED:
            raise ClinicalError("Only a completed consultation can be reopened.")
        EncounterRevision.objects.create(encounter=enc, revised_by=actor, reason=reason, snapshot=_snapshot(enc))
        enc.status, enc.completed_at = Encounter.Status.IN_PROGRESS, None
        enc.save()
        log_event(user=actor, action="encounter.reopen", obj=enc, request=request)  # reason stays in the clinical table
    return enc


# ---------------------------------------------------------------- prescriptions
def resolve_doctor_profile(actor):
    prof = DoctorProfile.objects.filter(user=actor, is_active=True).first()
    if prof is None:
        unlinked = list(DoctorProfile.objects.filter(is_active=True, user__isnull=True)[:2])
        if len(unlinked) == 1:
            prof = unlinked[0]
    return prof


def _rx_editable(rx, enc):
    if rx.status != RX.DRAFT:
        raise ClinicalError("This prescription has been issued. Use Correct to make a new version.")
    if enc.status != Encounter.Status.IN_PROGRESS and rx.supersedes_id is None:
        _editable(enc)


def _item_data(data):
    out = {k: data.get(k) for k in ("medicine", "medicine_name", "strength", "dosage_form", "dose", "route",
                                     "frequency", "meal_timing", "duration", "quantity", "instructions")}
    if out["meal_timing"] is None:  # callers that do not send it mean "not specified"
        out["meal_timing"] = ""
    return out


def add_item(*, actor, request, encounter_id, data):
    _require(actor, "clinical.prescribe")
    with transaction.atomic():
        enc = _lock_encounter(encounter_id)
        _check_patient(enc.patient)
        rx = Prescription.objects.select_for_update().filter(encounter=enc, status=RX.DRAFT).first()
        if rx is None:
            _editable(enc)
            if Prescription.objects.filter(encounter=enc, status=RX.FINAL).exists():
                raise ClinicalError("A prescription was already issued for this visit. Use Correct to change it.")
            last = Prescription.objects.filter(encounter=enc).order_by("-version").first()
            rx = Prescription.objects.create(encounter=enc, patient=enc.patient, created_by=actor,
                                             version=(last.version + 1) if last else 1)
        _rx_editable(rx, enc)
        line = (rx.items.order_by("-line_no").values_list("line_no", flat=True).first() or 0) + 1
        item = PrescriptionItem.objects.create(prescription=rx, line_no=line, **_item_data(data))
        log_event(user=actor, action="rx.item_add", obj=rx, request=request)
    return item


def update_item(*, actor, request, item_id, data):
    _require(actor, "clinical.prescribe")
    with transaction.atomic():
        item = PrescriptionItem.objects.select_related("prescription").get(pk=item_id)
        enc = _lock_encounter(item.prescription.encounter_id)
        rx = Prescription.objects.select_for_update().get(pk=item.prescription_id)
        _rx_editable(rx, enc)
        for k, v in _item_data(data).items():
            setattr(item, k, v)
        item.save()
        log_event(user=actor, action="rx.item_update", obj=rx, request=request)
    return item


def remove_item(*, actor, request, item_id):
    _require(actor, "clinical.prescribe")
    with transaction.atomic():
        item = PrescriptionItem.objects.select_related("prescription").get(pk=item_id)
        enc = _lock_encounter(item.prescription.encounter_id)
        rx = Prescription.objects.select_for_update().get(pk=item.prescription_id)
        _rx_editable(rx, enc)
        item.delete()
        log_event(user=actor, action="rx.item_remove", obj=rx, request=request)


def discard_draft(*, actor, request, prescription_id):
    _require(actor, "clinical.prescribe")
    with transaction.atomic():
        enc = _lock_encounter(Prescription.objects.only("encounter_id").get(pk=prescription_id).encounter_id)
        rx = Prescription.objects.select_for_update().get(pk=prescription_id)
        _rx_editable(rx, enc)
        pk = rx.pk
        rx.delete()
        log_event(user=actor, action="rx.discard_draft", summary=f"Draft {pk} discarded", request=request)


def allergy_alerts(prescription):
    """Matches only the allergy names recorded for this patient against the medicine names on the draft.
    It does NOT know drug classes, cross-reactions or interactions (no licensed dataset is loaded)."""
    allergies = list(Allergy.objects.filter(patient=prescription.patient, status=RecordStatus.ACTIVE))
    alerts = []
    for item in prescription.items.select_related("medicine"):
        parts = [item.medicine_name]
        if item.medicine_id:
            parts += [item.medicine.generic_name, item.medicine.brand_name]
        hay = normalize_name(" ".join(parts))
        for a in allergies:
            needle = normalize_name(a.allergen)
            if len(needle) >= 3 and needle in hay:
                alerts.append(f"{item.medicine_name} matches the recorded allergy '{a.allergen}' "
                              f"({a.get_severity_display()}).")
    return alerts


def finalize_prescription(*, actor, request, prescription_id, acknowledge=False):
    _require(actor, "clinical.prescribe")
    with transaction.atomic():
        # lock order is always: encounter first, then prescription (prevents deadlocks)
        enc = _lock_encounter(Prescription.objects.only("encounter_id").get(pk=prescription_id).encounter_id)
        rx = Prescription.objects.select_for_update(of=("self",)).select_related("patient").get(pk=prescription_id)
        _rx_editable(rx, enc)
        _check_patient(rx.patient)
        if not rx.items.exists():
            raise ClinicalError("Add at least one medicine first.")
        prof = resolve_doctor_profile(actor)
        if prof is None:
            raise ClinicalError("The doctor's profile is missing. Add it under Doctor profiles (clinic settings).")
        if Setting.get("require_doctor_registration_on_prescription", True) and not prof.registration_number.strip():
            raise ClinicalError("The doctor's registration number is not set in the doctor profile.")
        alerts = allergy_alerts(rx)
        if alerts and not acknowledge:
            raise ClinicalError("Review the allergy alerts and tick the box to confirm before issuing.")
        if rx.supersedes_id:
            previous = Prescription.objects.select_for_update().get(pk=rx.supersedes_id)
            if previous.status != RX.FINAL:
                raise ClinicalError("The prescription being corrected is no longer current.")
            previous.mark(RX.SUPERSEDED)
        number = next_number(DocType.PRESCRIPTION, period=f"{timezone.localdate():%Y}")
        rx.number, rx.status = number.formatted, RX.FINAL
        rx.finalized_at, rx.finalized_by = timezone.now(), actor
        rx.doctor_name, rx.doctor_qualifications = prof.full_name, prof.qualifications
        rx.doctor_registration_number, rx.doctor_registration_council = prof.registration_number, prof.registration_council
        rx.alerts_acknowledged, rx.alert_summary = bool(alerts), "\n".join(alerts)
        rx.save()
        log_event(user=actor, action="rx.finalize", obj=rx, summary=f"{rx.number} v{rx.version}",
                  changes={"alerts_acknowledged": bool(alerts)}, request=request)
    return rx


def start_correction(*, actor, request, prescription_id, reason):
    _require(actor, "clinical.authorize_correction")
    reason = _need_reason(reason)
    with transaction.atomic():
        _lock_encounter(Prescription.objects.only("encounter_id").get(pk=prescription_id).encounter_id)
        old = Prescription.objects.select_for_update(of=("self",)).get(pk=prescription_id)
        if old.status != RX.FINAL:
            raise ClinicalError("Only the current issued prescription can be corrected.")
        if Prescription.objects.filter(encounter_id=old.encounter_id, status=RX.DRAFT).exists():
            raise ClinicalError("There is already a draft for this visit. Finish or discard it first.")
        new = Prescription.objects.create(
            encounter_id=old.encounter_id, patient_id=old.patient_id, created_by=actor, version=old.version + 1,
            supersedes=old, correction_reason=reason)
        for item in old.items.all():
            PrescriptionItem.objects.create(
                prescription=new, line_no=item.line_no, medicine_id=item.medicine_id, medicine_name=item.medicine_name,
                strength=item.strength, dosage_form=item.dosage_form, dose=item.dose, route=item.route,
                frequency=item.frequency, meal_timing=item.meal_timing, duration=item.duration,
                quantity=item.quantity, instructions=item.instructions)
        log_event(user=actor, action="rx.start_correction", obj=new, summary=f"Correction of {old.number}", request=request)
    return new


def cancel_prescription(*, actor, request, prescription_id, reason):
    _require(actor, "clinical.authorize_correction")
    reason = _need_reason(reason)
    with transaction.atomic():
        rx = Prescription.objects.select_for_update().get(pk=prescription_id)
        if rx.status != RX.FINAL:
            raise ClinicalError("Only the current issued prescription can be cancelled.")
        rx.mark(RX.CANCELLED, cancel_reason=reason)
        log_event(user=actor, action="rx.cancel", obj=rx, summary=rx.number, request=request)
    return rx
