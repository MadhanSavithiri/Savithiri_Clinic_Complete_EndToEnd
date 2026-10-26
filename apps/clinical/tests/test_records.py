from django.core.exceptions import PermissionDenied
from django.db import DatabaseError, connection, transaction
from django.test import TestCase
import unittest

from apps.clinical import services
from apps.clinical.models import (Allergy, ClinicalNote, ClinicalProfile, Encounter, EncounterRevision,
                                  FollowUp, RecordStatus)
from apps.core.models import AuditEvent, ImmutableRecordError
from apps.patients import services as patient_services
from apps.queueing.models import QueueToken

from .helpers import World


class AllergyTests(TestCase):
    def setUp(self):
        self.w = World()

    def add(self, name="Penicillin", **kw):
        return services.add_allergy(actor=self.w.doctor, request=None, patient=self.w.patient, allergen=name, **kw)

    def test_unknown_then_allergies_then_nkda_rules(self):
        self.assertEqual(services.allergy_status(self.w.patient)[0], "UNKNOWN")
        services.set_no_known_allergies(actor=self.w.doctor, request=None, patient=self.w.patient)
        self.assertEqual(services.allergy_status(self.w.patient)[0], "NKDA")
        self.add()  # adding an allergy cancels "no known allergies"
        state, active = services.allergy_status(self.w.patient)
        self.assertEqual((state, len(active)), ("ALLERGIES", 1))
        with self.assertRaises(services.ClinicalError):
            services.set_no_known_allergies(actor=self.w.doctor, request=None, patient=self.w.patient)

    def test_duplicate_allergy_refused_ignoring_case_and_spacing(self):
        self.add("Penicillin")
        with self.assertRaises(services.ClinicalError):
            self.add("  PENICILLIN ")

    def test_resolved_or_error_records_are_kept_not_deleted(self):
        a = self.add()
        services.set_record_status(actor=self.w.doctor, request=None, kind="allergy", record_id=a.pk, status="ERROR")
        a.refresh_from_db()
        self.assertEqual(a.status, RecordStatus.ENTERED_IN_ERROR)
        self.assertEqual(services.allergy_status(self.w.patient)[0], "UNKNOWN")
        self.assertEqual(Allergy.objects.count(), 1)

    def test_only_the_doctor_may_record(self):
        for user in (self.w.reception, self.w.pharmacy, self.w.admin):
            with self.assertRaises(PermissionDenied):
                services.add_allergy(actor=user, request=None, patient=self.w.patient, allergen="x")

    def test_audit_holds_no_allergen_text(self):
        self.add("Sulfonamide")
        blob = " ".join(f"{e.summary} {e.object_repr} {e.changes}" for e in AuditEvent.objects.filter(action="allergy.add"))
        self.assertNotIn("Sulfonamide", blob)

    def test_merge_clears_nkda_when_allergies_arrive(self):
        other = make_other(self.w)
        services.set_no_known_allergies(actor=self.w.doctor, request=None, patient=self.w.patient)
        services.add_allergy(actor=self.w.doctor, request=None, patient=other, allergen="Penicillin")
        patient_services.merge_patients(actor=self.w.admin, request=None, target=self.w.patient, source=other, reason="dup")
        self.assertEqual(services.allergy_status(self.w.patient)[0], "ALLERGIES")
        self.assertFalse(ClinicalProfile.objects.get(patient=self.w.patient).no_known_allergies)


def make_other(w):
    from apps.patients.tests.helpers import make_patient
    return make_patient(w.reception, name="Anbu Selvan", phone="9000000009")


class ConsultationFlowTests(TestCase):
    def setUp(self):
        self.w = World()

    def test_start_creates_encounter_calls_token_and_is_idempotent(self):
        token = self.w.token()
        enc = services.start_consultation(actor=self.w.doctor, request=None, token_id=token.pk)
        token.refresh_from_db()
        self.assertEqual(token.status, "IN_CONSULTATION")
        self.assertEqual((enc.patient, enc.doctor, enc.status), (self.w.patient, self.w.doctor, "IN_PROGRESS"))
        again = services.start_consultation(actor=self.w.doctor, request=None, token_id=token.pk)
        self.assertEqual(again.pk, enc.pk)
        self.assertEqual(Encounter.objects.count(), 1)

    def test_only_doctor_can_start_and_cancelled_token_cannot(self):
        token = self.w.token()
        for user in (self.w.reception, self.w.pharmacy, self.w.admin):
            with self.assertRaises(PermissionDenied):
                services.start_consultation(actor=user, request=None, token_id=token.pk)
        from apps.queueing import services as qs
        qs.change_token_status(actor=self.w.reception, request=None, token_id=token.pk, action="cancel", reason="left")
        with self.assertRaises(services.ClinicalError):
            services.start_consultation(actor=self.w.doctor, request=None, token_id=token.pk)

    def test_notes_save_and_audit_has_no_clinical_text(self):
        enc = self.w.encounter()
        self.w.fill_notes(enc, presenting_complaints="Secret complaint text")
        enc.refresh_from_db()
        self.assertEqual(enc.presenting_complaints, "Secret complaint text")
        ev = AuditEvent.objects.get(action="encounter.update")
        self.assertIn("presenting_complaints", ev.summary)
        self.assertNotIn("Secret", f"{ev.summary} {ev.changes} {ev.object_repr}")

    def test_complete_requires_content_and_closes_the_token(self):
        enc = self.w.encounter()
        with self.assertRaises(services.ClinicalError):
            services.complete_consultation(actor=self.w.doctor, request=None, encounter_id=enc.pk)
        self.w.fill_notes(enc, assessment="")
        with self.assertRaises(services.ClinicalError):  # complaint but no diagnosis/assessment
            services.complete_consultation(actor=self.w.doctor, request=None, encounter_id=enc.pk)
        services.add_diagnosis(actor=self.w.doctor, request=None, encounter_id=enc.pk, description="Viral fever")
        services.complete_consultation(actor=self.w.doctor, request=None, encounter_id=enc.pk)
        enc.refresh_from_db()
        self.assertEqual(enc.status, "COMPLETED")
        self.assertIsNotNone(enc.completed_at)
        self.assertEqual(QueueToken.objects.get().status, "COMPLETED")

    def test_cannot_complete_with_open_draft_prescription(self):
        enc = self.w.encounter()
        self.w.fill_notes(enc)
        self.w.add_item(enc)
        with self.assertRaises(services.ClinicalError):
            services.complete_consultation(actor=self.w.doctor, request=None, encounter_id=enc.pk)

    def test_completed_consultation_is_read_only_but_accepts_addendum(self):
        enc = self.w.encounter()
        self.w.fill_notes(enc)
        services.complete_consultation(actor=self.w.doctor, request=None, encounter_id=enc.pk)
        with self.assertRaises(services.ClinicalError):
            self.w.fill_notes(enc, advice="changed")
        note = services.add_note(actor=self.w.doctor, request=None, encounter_id=enc.pk, text="Patient called: better")
        self.assertEqual(note.kind, "ADDENDUM")

    def test_follow_up_is_created_updated_and_closed_by_next_visit(self):
        from datetime import timedelta
        from django.utils import timezone
        due = timezone.localdate() + timedelta(days=7)
        enc = self.w.encounter()
        self.w.fill_notes(enc, follow_up_date=due, follow_up_notes="Review")
        services.complete_consultation(actor=self.w.doctor, request=None, encounter_id=enc.pk)
        fu = FollowUp.objects.get()
        self.assertEqual((fu.due_date, fu.status, fu.patient), (due, "PENDING", self.w.patient))
        self.w.encounter()  # patient comes back and a new visit starts
        fu.refresh_from_db()
        self.assertEqual(fu.status, "DONE")

    def test_reopen_keeps_a_snapshot_with_reason_and_needs_permission(self):
        enc = self.w.encounter()
        self.w.fill_notes(enc, presenting_complaints="Original text")
        services.complete_consultation(actor=self.w.doctor, request=None, encounter_id=enc.pk)
        with self.assertRaises(PermissionDenied):
            services.reopen_consultation(actor=self.w.reception, request=None, encounter_id=enc.pk, reason="x")
        with self.assertRaises(services.ClinicalError):
            services.reopen_consultation(actor=self.w.doctor, request=None, encounter_id=enc.pk, reason=" ")
        services.reopen_consultation(actor=self.w.doctor, request=None, encounter_id=enc.pk, reason="Typo in complaint")
        enc.refresh_from_db()
        self.assertEqual(enc.status, "IN_PROGRESS")
        self.w.fill_notes(enc, presenting_complaints="Corrected text")
        rev = EncounterRevision.objects.get()
        self.assertEqual(rev.snapshot["presenting_complaints"], "Original text")
        self.assertEqual((rev.reason, rev.revised_by), ("Typo in complaint", self.w.doctor))
        self.assertTrue(AuditEvent.objects.filter(action="encounter.reopen").exists())

    def test_vitals_conversion_voiding_and_limits(self):
        from apps.clinical.forms import VitalsForm
        enc = self.w.encounter()
        form = VitalsForm({"temperature": "98.6", "temperature_unit": "F", "systolic": "120", "diastolic": "80",
                           "weight_kg": "70", "height_cm": "170", "pulse": "72"})
        self.assertTrue(form.is_valid(), form.errors)
        v = services.record_vitals(actor=self.w.doctor, request=None, encounter_id=enc.pk, data=form.cleaned_data)
        self.assertEqual(str(v.temperature_c), "37.0")
        self.assertEqual(str(v.temperature_f), "98.6")
        self.assertEqual(str(v.bmi), "24.2")
        services.void_vitals(actor=self.w.doctor, request=None, vital_id=v.pk, reason="Wrong patient")
        v.refresh_from_db()
        self.assertTrue(v.is_void)
        for bad in ({"systolic": "80", "diastolic": "120"}, {"systolic": "120"}, {"pulse": "900"},
                    {"temperature": "150", "temperature_unit": "F"}, {}):
            f = VitalsForm({**bad})
            self.assertFalse(f.is_valid(), bad)

    def test_diagnosis_primary_swap_and_void(self):
        enc = self.w.encounter()
        a = services.add_diagnosis(actor=self.w.doctor, request=None, encounter_id=enc.pk, description="A", is_primary=True)
        b = services.add_diagnosis(actor=self.w.doctor, request=None, encounter_id=enc.pk, description="B", is_primary=True)
        a.refresh_from_db()
        self.assertFalse(a.is_primary)
        services.void_diagnosis(actor=self.w.doctor, request=None, diagnosis_id=b.pk)
        self.assertEqual(enc.diagnoses.filter(is_void=False).count(), 1)

    def test_vitals_and_diagnoses_cannot_change_after_completion(self):
        enc = self.w.encounter()
        self.w.fill_notes(enc)
        services.complete_consultation(actor=self.w.doctor, request=None, encounter_id=enc.pk)
        with self.assertRaises(services.ClinicalError):
            services.add_diagnosis(actor=self.w.doctor, request=None, encounter_id=enc.pk, description="late")


class ImmutabilityTests(TestCase):
    def setUp(self):
        self.w = World()
        self.enc = self.w.encounter()
        self.w.fill_notes(self.enc)
        self.note = services.add_note(actor=self.w.doctor, request=None, encounter_id=self.enc.pk, text="keep me")

    def test_notes_cannot_be_edited_or_deleted_through_the_orm(self):
        self.note.text = "tampered"
        with self.assertRaises(ImmutableRecordError):
            self.note.save()
        with self.assertRaises(ImmutableRecordError):
            self.note.delete()
        with self.assertRaises(ImmutableRecordError):
            ClinicalNote.objects.all().update(text="x")
        with self.assertRaises(ImmutableRecordError):
            ClinicalNote.objects.all().delete()

    @unittest.skipUnless(connection.vendor == "postgresql", "Triggers exist only on PostgreSQL")
    def test_database_blocks_raw_sql_changes(self):
        services.complete_consultation(actor=self.w.doctor, request=None, encounter_id=self.enc.pk)
        services.reopen_consultation(actor=self.w.doctor, request=None, encounter_id=self.enc.pk, reason="r")
        for table in ("clinical_clinicalnote", "clinical_encounterrevision"):
            for sql in (f"UPDATE {table} SET id = id", f"DELETE FROM {table}"):
                with self.assertRaises(DatabaseError):
                    with transaction.atomic():
                        with connection.cursor() as cur:
                            cur.execute(sql)
        self.assertEqual(ClinicalNote.objects.count(), 1)
        self.assertEqual(EncounterRevision.objects.count(), 1)
