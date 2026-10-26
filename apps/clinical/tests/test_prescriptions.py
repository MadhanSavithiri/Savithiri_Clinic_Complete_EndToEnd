import threading
import unittest
from datetime import date

from django.core.exceptions import PermissionDenied
from django.db import connection, connections
from django.test import TestCase, TransactionTestCase
from django.utils import timezone

from apps.clinical import services
from apps.clinical.models import Prescription, PrescriptionItem, PrescriptionLockedError
from apps.core.models import AuditEvent, DoctorProfile, Setting
from apps.patients.tests.helpers import make_patient

from .helpers import World, make_medicine


class DraftTests(TestCase):
    def setUp(self):
        self.w = World()
        self.enc = self.w.encounter()

    def test_free_text_item_creates_a_draft_without_a_number(self):
        item = self.w.add_item(self.enc)
        rx = item.prescription
        self.assertEqual((rx.status, rx.number, rx.version), ("DRAFT", None, 1))
        self.assertEqual(item.line_no, 1)
        self.assertEqual(self.w.add_item(self.enc, medicine_name="Second").line_no, 2)
        self.assertEqual(Prescription.objects.count(), 1)

    def test_catalogue_medicine_is_linked(self):
        med = make_medicine()
        item = self.w.add_item(self.enc, medicine=med, medicine_name=med.brand_name)
        self.assertEqual(item.medicine, med)

    def test_item_form_fills_from_catalogue_and_needs_a_name(self):
        from apps.clinical.forms import PrescriptionItemForm
        med = make_medicine()
        base = {"dose": "1", "route": "Oral", "frequency": "1-0-1", "duration": "5 days", "quantity": "10"}
        form = PrescriptionItemForm({**base, "medicine": med.pk})
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data["medicine_name"], "TestBrand")
        self.assertEqual(form.cleaned_data["strength"], "250 mg")
        self.assertFalse(PrescriptionItemForm({**base}).is_valid())
        self.assertFalse(PrescriptionItemForm({**base, "medicine_name": "X", "quantity": "0"}).is_valid())

    def test_update_and_remove_while_draft(self):
        item = self.w.add_item(self.enc)
        services.update_item(actor=self.w.doctor, request=None, item_id=item.pk, data=self.w.item_data(dose="2 tablets"))
        item.refresh_from_db()
        self.assertEqual(item.dose, "2 tablets")
        services.remove_item(actor=self.w.doctor, request=None, item_id=item.pk)
        self.assertEqual(PrescriptionItem.objects.count(), 0)

    def test_discard_draft(self):
        item = self.w.add_item(self.enc)
        services.discard_draft(actor=self.w.doctor, request=None, prescription_id=item.prescription_id)
        self.assertEqual((Prescription.objects.count(), PrescriptionItem.objects.count()), (0, 0))

    def test_only_the_doctor_can_prescribe(self):
        for user in (self.w.reception, self.w.pharmacy, self.w.admin):
            with self.assertRaises(PermissionDenied):
                services.add_item(actor=user, request=None, encounter_id=self.enc.pk, data=self.w.item_data())
        self.assertEqual(Prescription.objects.count(), 0)


class FinalizeTests(TestCase):
    def setUp(self):
        self.w = World()
        self.enc = self.w.encounter()

    def test_issue_assigns_number_snapshots_doctor_and_audits(self):
        rx = self.w.issue(self.enc)
        year = timezone.localdate().year
        self.assertEqual((rx.status, rx.number), ("FINAL", f"RX-{year}-000001"))
        self.assertEqual((rx.doctor_name, rx.doctor_registration_number), ("Dr TEST Doctor", "TEST-REG-0001"))
        self.assertEqual(rx.finalized_by, self.w.doctor)
        DoctorProfile.objects.update(full_name="Renamed Later")  # old prescriptions must not change
        rx.refresh_from_db()
        self.assertEqual(rx.doctor_name, "Dr TEST Doctor")
        ev = AuditEvent.objects.get(action="rx.finalize")
        self.assertIn(rx.number, ev.summary)
        self.assertNotIn("Test Tablet", f"{ev.summary} {ev.changes}")

    def test_numbers_increase(self):
        first = self.w.issue(self.enc)
        enc2 = self.w.encounter(make_patient(self.w.reception, name="Meena Devi", phone="9000000002"))
        second = self.w.issue(enc2)
        self.assertEqual((first.number[-6:], second.number[-6:]), ("000001", "000002"))

    def test_needs_items_and_doctor_profile_with_registration(self):
        item = self.w.add_item(self.enc)
        services.remove_item(actor=self.w.doctor, request=None, item_id=item.pk)
        with self.assertRaises(services.ClinicalError):
            services.finalize_prescription(actor=self.w.doctor, request=None, prescription_id=item.prescription_id)
        item = self.w.add_item(self.enc)
        DoctorProfile.objects.update(registration_number="")
        with self.assertRaises(services.ClinicalError) as ctx:
            services.finalize_prescription(actor=self.w.doctor, request=None, prescription_id=item.prescription_id)
        self.assertIn("registration number", str(ctx.exception))
        Setting.objects.create(key="require_doctor_registration_on_prescription", value=False)
        self.assertEqual(services.finalize_prescription(actor=self.w.doctor, request=None,
                                                        prescription_id=item.prescription_id).status, "FINAL")

    def test_missing_doctor_profile_blocks_issue(self):
        item = self.w.add_item(self.enc)
        DoctorProfile.objects.all().delete()
        with self.assertRaises(services.ClinicalError):
            services.finalize_prescription(actor=self.w.doctor, request=None, prescription_id=item.prescription_id)

    def test_issued_prescription_cannot_be_edited_anywhere(self):
        rx = self.w.issue(self.enc)
        item = rx.items.get()
        with self.assertRaises(services.ClinicalError):
            self.w.add_item(self.enc)
        with self.assertRaises(services.ClinicalError):
            services.update_item(actor=self.w.doctor, request=None, item_id=item.pk, data=self.w.item_data())
        with self.assertRaises(services.ClinicalError):
            services.remove_item(actor=self.w.doctor, request=None, item_id=item.pk)
        with self.assertRaises(services.ClinicalError):
            services.discard_draft(actor=self.w.doctor, request=None, prescription_id=rx.pk)
        # and the model layer refuses too, even if a service were bypassed
        rx.number = "HACKED"
        with self.assertRaises(PrescriptionLockedError):
            rx.save()
        item.dose = "100 tablets"
        with self.assertRaises(PrescriptionLockedError):
            item.save()
        with self.assertRaises(PrescriptionLockedError):
            item.delete()
        with self.assertRaises(PrescriptionLockedError):
            Prescription.objects.get(pk=rx.pk).delete()
        self.assertEqual(Prescription.objects.get(pk=rx.pk).number[:3], "RX-")


class AllergyAlertTests(TestCase):
    def setUp(self):
        self.w = World()
        self.enc = self.w.encounter()
        services.add_allergy(actor=self.w.doctor, request=None, patient=self.w.patient, allergen="Amoxicillin",
                             severity="SEVERE")

    def test_matching_allergy_needs_acknowledgement(self):
        item = self.w.add_item(self.enc, medicine_name="Amoxicillin")
        rx = item.prescription
        self.assertEqual(len(services.allergy_alerts(rx)), 1)
        with self.assertRaises(services.ClinicalError):
            services.finalize_prescription(actor=self.w.doctor, request=None, prescription_id=rx.pk)
        done = services.finalize_prescription(actor=self.w.doctor, request=None, prescription_id=rx.pk, acknowledge=True)
        self.assertTrue(done.alerts_acknowledged)
        self.assertIn("Amoxicillin", done.alert_summary)

    def test_generic_name_in_catalogue_is_matched(self):
        med = make_medicine(brand_name="Brandx", generic_name="Amoxicillin")
        item = self.w.add_item(self.enc, medicine=med, medicine_name="Brandx")
        self.assertEqual(len(services.allergy_alerts(item.prescription)), 1)

    def test_no_alert_when_names_differ_and_resolved_allergies_ignored(self):
        item = self.w.add_item(self.enc, medicine_name="Paracetamol")
        self.assertEqual(services.allergy_alerts(item.prescription), [])
        item2 = self.w.add_item(self.enc, medicine_name="Amoxicillin")
        services.set_record_status(actor=self.w.doctor, request=None, kind="allergy",
                                   record_id=self.w.patient.allergies.get().pk, status="RESOLVED")
        self.assertEqual(services.allergy_alerts(item2.prescription), [])


class CorrectionTests(TestCase):
    def setUp(self):
        self.w = World()
        self.enc = self.w.encounter()
        self.w.fill_notes(self.enc)
        self.rx = self.w.issue(self.enc)

    def test_correction_creates_draft_copy_and_supersedes_on_issue(self):
        with self.assertRaises(services.ClinicalError):
            services.start_correction(actor=self.w.doctor, request=None, prescription_id=self.rx.pk, reason=" ")
        draft = services.start_correction(actor=self.w.doctor, request=None, prescription_id=self.rx.pk, reason="Wrong dose")
        self.assertEqual((draft.status, draft.version, draft.supersedes), ("DRAFT", 2, self.rx))
        self.assertEqual(draft.items.count(), 1)
        self.assertEqual(Prescription.objects.get(pk=self.rx.pk).status, "FINAL")  # still valid until replaced
        item = draft.items.get()
        services.update_item(actor=self.w.doctor, request=None, item_id=item.pk, data=self.w.item_data(dose="2 tablets"))
        new = services.finalize_prescription(actor=self.w.doctor, request=None, prescription_id=draft.pk)
        self.assertEqual((new.status, new.version), ("FINAL", 2))
        self.assertNotEqual(new.number, self.rx.number)
        old = Prescription.objects.get(pk=self.rx.pk)
        self.assertEqual(old.status, "SUPERSEDED")
        self.assertEqual(old.items.get().dose, "1 tablet")  # original wording preserved

    def test_correction_works_after_the_consultation_is_completed(self):
        services.complete_consultation(actor=self.w.doctor, request=None, encounter_id=self.enc.pk)
        draft = services.start_correction(actor=self.w.doctor, request=None, prescription_id=self.rx.pk, reason="Qty")
        item = draft.items.get()
        services.update_item(actor=self.w.doctor, request=None, item_id=item.pk, data=self.w.item_data(quantity=9))
        self.assertEqual(services.finalize_prescription(actor=self.w.doctor, request=None, prescription_id=draft.pk).status, "FINAL")

    def test_only_one_draft_and_only_current_prescription_can_be_corrected(self):
        draft = services.start_correction(actor=self.w.doctor, request=None, prescription_id=self.rx.pk, reason="a")
        with self.assertRaises(services.ClinicalError):
            services.start_correction(actor=self.w.doctor, request=None, prescription_id=self.rx.pk, reason="b")
        services.finalize_prescription(actor=self.w.doctor, request=None, prescription_id=draft.pk)
        with self.assertRaises(services.ClinicalError):
            services.start_correction(actor=self.w.doctor, request=None, prescription_id=self.rx.pk, reason="c")

    def test_discarding_a_correction_draft_keeps_the_original(self):
        draft = services.start_correction(actor=self.w.doctor, request=None, prescription_id=self.rx.pk, reason="a")
        services.discard_draft(actor=self.w.doctor, request=None, prescription_id=draft.pk)
        self.assertEqual(Prescription.objects.get(pk=self.rx.pk).status, "FINAL")

    def test_cancel_needs_reason_and_permission(self):
        for user in (self.w.reception, self.w.pharmacy):
            with self.assertRaises(PermissionDenied):
                services.cancel_prescription(actor=user, request=None, prescription_id=self.rx.pk, reason="x")
        with self.assertRaises(services.ClinicalError):
            services.cancel_prescription(actor=self.w.doctor, request=None, prescription_id=self.rx.pk, reason="")
        services.cancel_prescription(actor=self.w.doctor, request=None, prescription_id=self.rx.pk, reason="Issued in error")
        rx = Prescription.objects.get(pk=self.rx.pk)
        self.assertEqual((rx.status, rx.cancel_reason), ("CANCELLED", "Issued in error"))
        self.assertEqual(rx.items.count(), 1)


@unittest.skipUnless(connection.vendor == "postgresql", "Concurrency tests need PostgreSQL")
class ConcurrentPrescriptionTests(TransactionTestCase):
    def setUp(self):
        self.w = World()

    def run_threads(self, targets):
        results, errors = [], []
        barrier = threading.Barrier(len(targets))

        def worker(fn):
            try:
                barrier.wait()
                results.append(fn())
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)
            finally:
                connections.close_all()

        threads = [threading.Thread(target=worker, args=(fn,)) for fn in targets]
        [t.start() for t in threads]
        [t.join() for t in threads]
        return results, errors

    def test_eight_simultaneous_issues_get_unique_numbers(self):
        drafts = []
        for i in range(8):
            p = make_patient(self.w.reception, name=f"Patient Number{i}", phone=f"90000{i:05d}")
            drafts.append(self.w.add_item(self.w.encounter(p)).prescription_id)
        targets = [lambda d=d: services.finalize_prescription(actor=self.w.doctor, request=None, prescription_id=d).number
                   for d in drafts]
        results, errors = self.run_threads(targets)
        self.assertEqual(errors, [])
        self.assertEqual(len(set(results)), 8)
        self.assertEqual(sorted(int(n[-6:]) for n in results), list(range(1, 9)))

    def test_double_clicked_issue_succeeds_once(self):
        draft = self.w.add_item(self.w.encounter()).prescription_id
        issue = lambda: services.finalize_prescription(actor=self.w.doctor, request=None, prescription_id=draft)  # noqa: E731
        results, errors = self.run_threads([issue, issue, issue])
        self.assertEqual(len(results), 1)
        self.assertEqual(len(errors), 2)
        self.assertEqual(Prescription.objects.filter(status="FINAL").count(), 1)
