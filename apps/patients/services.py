"""Patient business rules. Every change is atomic and audited. Audit entries hold patient IDs and the
NAMES of changed fields, never the personal values themselves."""
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone

from apps.core.audit import log_event
from apps.core.models import DocType
from apps.core.sequences import next_number

from .models import Patient, PatientIdentifier, PatientMergeLog, Sex
from .signals import patient_merged
from .normalize import normalize_name

DATA_FIELDS = [
    "full_name", "sex", "date_of_birth", "dob_estimated", "phone", "address",
    "emergency_contact_name", "emergency_contact_phone", "emergency_contact_relation",
    "consent_given", "consent_given_by",
]


class PatientError(Exception):
    """A rule was broken; the message is safe to show to staff."""


def register_patient(*, actor, request, data):
    with transaction.atomic():
        number = next_number(DocType.PATIENT)
        patient = Patient(serial=number.value, patient_id=number.formatted, created_by=actor)
        for field in DATA_FIELDS:
            setattr(patient, field, data.get(field, getattr(patient, field)))
        if patient.consent_given:
            patient.consent_recorded_at = timezone.now()
            patient.consent_recorded_by = actor
        patient.save()
        log_event(user=actor, action="patient.create", obj=patient,
                  summary=f"Registered {patient.patient_id}", request=request)
    return patient


def update_patient(*, actor, request, patient, data):
    with transaction.atomic():
        patient = Patient.objects.select_for_update().get(pk=patient.pk)
        if not patient.is_active:
            raise PatientError("This record was merged into another patient and can no longer be edited.")
        changed = [f for f in DATA_FIELDS if f in data and getattr(patient, f) != data[f]]
        if not changed:
            return patient
        consent_before = patient.consent_given
        for f in changed:
            setattr(patient, f, data[f])
        if patient.consent_given and not consent_before:
            patient.consent_recorded_at = timezone.now()
            patient.consent_recorded_by = actor
        patient.save()
        log_event(user=actor, action="patient.update", obj=patient,
                  summary="Fields changed: " + ", ".join(changed), request=request)
        if consent_before and not patient.consent_given:
            log_event(user=actor, action="patient.consent_withdrawn", obj=patient, request=request)
    return patient


def add_identifier(*, actor, request, patient, id_type, value):
    try:
        with transaction.atomic():
            ident = PatientIdentifier.objects.create(patient=patient, id_type=id_type, value=value, created_by=actor)
            log_event(user=actor, action="patient.identifier_add", obj=patient,
                      summary=f"Identifier type {id_type} added", request=request)
    except IntegrityError:
        raise PatientError("That identifier is already recorded for a patient.")
    return ident


def find_possible_duplicates(*, full_name, phone="", dob=None, exclude_pk=None):
    """Soft duplicate check: returns up to 5 (patient, reason). Family members share phones, so a phone
    alone is never enough; staff can always confirm 'this is a new patient'."""
    name = normalize_name(full_name)
    if not name:
        return []
    first = name.split()[0]
    qs = Patient.objects.filter(is_active=True)
    if exclude_pk:
        qs = qs.exclude(pk=exclude_pk)
    found = {}
    for p in qs.filter(name_search=name):
        if dob and p.date_of_birth == dob:
            found.setdefault(p.pk, (p, "Same name and date of birth"))
        elif phone and p.phone == phone:
            found.setdefault(p.pk, (p, "Same name and phone number"))
        else:
            conflict = (dob and p.date_of_birth and dob != p.date_of_birth) or (phone and p.phone and phone != p.phone)
            if not conflict:
                found.setdefault(p.pk, (p, "Same name"))
    if phone:
        for p in qs.filter(phone=phone).exclude(name_search=name):
            if p.name_search.split()[:1] == [first]:
                found.setdefault(p.pk, (p, "Same phone number and first name"))
    return list(found.values())[:5]


def search_patients(query, limit=50):
    """Search by patient ID/number, name words (any order), phone digits or another identifier."""
    q = (query or "").strip()
    qs = Patient.objects.filter(is_active=True)
    if not q:
        return qs.order_by("-serial")[:25]
    cond = Q(patient_id__iexact=q) | Q(identifiers__value__iexact=q)
    digits = "".join(ch for ch in q if ch.isdigit())
    if q.isdigit() and len(q) <= 9:
        cond |= Q(serial=int(q))
    if len(digits) >= 4:
        cond |= Q(phone__contains=digits)
    words = normalize_name(q).split()
    if words:
        name_cond = Q()
        for w in words:
            name_cond &= Q(name_search__contains=w)
        cond |= name_cond
    return qs.filter(cond).distinct().order_by("-serial")[:limit]


def merge_patients(*, actor, request, target, source, reason):
    """Merge `source` (duplicate) INTO `target` (kept). Everything linked to source moves to target."""
    if target.pk == source.pk:
        raise PatientError("Choose two different records.")
    try:
        with transaction.atomic():
            locked = {p.pk: p for p in Patient.objects.select_for_update().filter(pk__in=[target.pk, source.pk]).order_by("pk")}
            target, source = locked[target.pk], locked[source.pk]
            if not (target.is_active and source.is_active):
                raise PatientError("Both records must be current (not already merged).")

            moved = {}
            for rel in Patient._meta.related_objects:
                model = rel.related_model
                if model in (Patient, PatientIdentifier, PatientMergeLog) or not rel.one_to_many:
                    continue
                count = model._default_manager.filter(**{rel.field.name: source}).update(**{rel.field.name: target})
                if count:
                    moved[model._meta.label] = count

            existing = set(target.identifiers.values_list("id_type", "value"))
            moved_ids = 0
            for ident in source.identifiers.all():
                if (ident.id_type, ident.value) not in existing:
                    ident.patient = target
                    ident.save(update_fields=["patient", "updated_at"])
                    moved_ids += 1
            if moved_ids:
                moved["patients.PatientIdentifier"] = moved_ids

            # Fill blanks on the kept record from the duplicate; never overwrite what is there.
            for f in ("phone", "address", "emergency_contact_name", "emergency_contact_phone", "emergency_contact_relation"):
                if not getattr(target, f) and getattr(source, f):
                    setattr(target, f, getattr(source, f))
            if target.date_of_birth is None and source.date_of_birth:
                target.date_of_birth, target.dob_estimated = source.date_of_birth, source.dob_estimated
            if target.sex == Sex.UNKNOWN and source.sex != Sex.UNKNOWN:
                target.sex = source.sex
            target.save()

            source.is_active = False
            source.merged_into = target
            source.save(update_fields=["is_active", "merged_into", "updated_at"])
            patient_merged.send(sender=Patient, target=target, source=source, moved=moved, actor=actor)
            PatientMergeLog.objects.create(source=source, target=target, merged_by=actor, reason=reason, moved_counts=moved)
            log_event(user=actor, action="patient.merge", obj=target,
                      summary=f"{source.patient_id} merged into {target.patient_id}: {reason}",
                      changes={"moved": moved, "source": source.patient_id}, request=request)
    except IntegrityError:
        raise PatientError("Both records have an open queue entry today. Complete or cancel one of them first.")
    return target
