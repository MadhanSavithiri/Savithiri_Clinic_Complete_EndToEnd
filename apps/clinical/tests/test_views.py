from datetime import timedelta

from django.contrib.messages import get_messages
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from apps.clinical import services
from apps.clinical.models import Allergy, Encounter, Prescription, RecordStatus
from apps.core.models import AuditEvent
from apps.patients.tests.helpers import make_patient

from .helpers import World, make_medicine


def msgs(r):
    return [str(m) for m in get_messages(r.wsgi_request)]


class ViewBase(TestCase):
    def setUp(self):
        self.w = World()
        self.client.force_login(self.w.doctor)


class AccessTests(ViewBase):
    def setUp(self):
        super().setUp()
        self.enc = self.w.encounter()
        self.rx = self.w.issue(self.enc)
        self.get_urls = [
            reverse("clinical:dashboard"), reverse("clinical:record", args=[self.w.patient.pk]),
            reverse("clinical:encounter", args=[self.enc.pk]), reverse("clinical:rx_print", args=[self.rx.pk]),
            reverse("clinical:rx_correct", args=[self.rx.pk]), reverse("clinical:rx_cancel", args=[self.rx.pk]),
            reverse("clinical:reopen", args=[self.enc.pk]),
        ]
        e = self.enc.pk
        self.post_urls = [
            reverse("clinical:notes_save", args=[e]), reverse("clinical:vitals_add", args=[e]),
            reverse("clinical:diagnosis_add", args=[e]), reverse("clinical:allergy_add", args=[e]),
            reverse("clinical:allergy_nkda", args=[e]), reverse("clinical:history_add", args=[e]),
            reverse("clinical:medicine_add", args=[e]), reverse("clinical:referral_add", args=[e]),
            reverse("clinical:note_add", args=[e]), reverse("clinical:complete", args=[e]),
            reverse("clinical:rx_item_add", args=[e]), reverse("clinical:rx_discard", args=[self.rx.pk]),
            reverse("clinical:start", args=[self.enc.queue_token_id]),
        ]

    def test_anonymous_goes_to_login(self):
        self.client.logout()
        for url in self.get_urls:
            self.assertEqual(self.client.get(url).status_code, 302, url)

    def test_only_the_doctor_may_open_clinical_pages(self):
        self.assertTrue(all(self.client.get(u).status_code == 200 for u in self.get_urls[:4]))
        for user in (self.w.reception, self.w.pharmacy, self.w.admin):
            self.client.force_login(user)
            for url in self.get_urls:
                self.assertEqual(self.client.get(url).status_code, 403, f"{user.role} {url}")

    def test_other_roles_cannot_post_anything(self):
        before = (Prescription.objects.count(), Allergy.objects.count(), AuditEvent.objects.filter(action="note.add").count())
        for user in (self.w.reception, self.w.pharmacy, self.w.admin):
            self.client.force_login(user)
            for url in self.post_urls:
                self.assertEqual(self.client.post(url, {"text": "x", "allergen": "x"}).status_code, 403, f"{user.role} {url}")
        self.assertEqual(before, (Prescription.objects.count(), Allergy.objects.count(),
                                  AuditEvent.objects.filter(action="note.add").count()))

    def test_reception_cannot_even_see_the_encounter_text(self):
        self.w.fill_notes(self.enc, presenting_complaints="Distinctive complaint text")
        self.client.force_login(self.w.reception)
        r = self.client.get(reverse("clinical:encounter", args=[self.enc.pk]))
        self.assertNotContains(r, "Distinctive", status_code=403)


class EncounterPageTests(ViewBase):
    def setUp(self):
        super().setUp()
        self.enc = self.w.encounter()
        self.url = reverse("clinical:encounter", args=[self.enc.pk])

    def test_allergy_banner_states(self):
        self.assertContains(self.client.get(self.url), "Allergies not recorded yet")
        self.client.post(reverse("clinical:allergy_nkda", args=[self.enc.pk]))
        self.assertContains(self.client.get(self.url), "No known allergies (confirmed)")
        self.client.post(reverse("clinical:allergy_add", args=[self.enc.pk]),
                         {"allergen": "Penicillin", "reaction": "Rash", "severity": "SEVERE"})
        r = self.client.get(self.url)
        self.assertContains(r, "Penicillin")
        self.assertNotContains(r, "No known allergies (confirmed)")

    def test_viewing_is_audited_without_clinical_text(self):
        self.w.fill_notes(self.enc, presenting_complaints="Private complaint")
        self.client.get(self.url)
        ev = AuditEvent.objects.filter(action="clinical.view", username="doc").latest("id")
        self.assertNotIn("Private", f"{ev.summary} {ev.object_repr} {ev.changes}")

    def test_notes_save_and_past_followup_rejected(self):
        data = {"presenting_complaints": "Cough", "symptom_duration": "3 days", "examination_findings": "",
                "assessment": "URTI", "treatment_plan": "", "advice": "Steam", "follow_up_notes": ""}
        r = self.client.post(reverse("clinical:notes_save", args=[self.enc.pk]), {**data, "follow_up_date": "2000-01-01"})
        self.assertContains(r, "cannot be in the past")
        self.enc.refresh_from_db()
        self.assertEqual(self.enc.presenting_complaints, "")
        tomorrow = (timezone.localdate() + timedelta(days=1)).isoformat()
        r = self.client.post(reverse("clinical:notes_save", args=[self.enc.pk]), {**data, "follow_up_date": tomorrow})
        self.assertEqual(r.status_code, 302)
        self.enc.refresh_from_db()
        self.assertEqual((self.enc.presenting_complaints, str(self.enc.follow_up_date)), ("Cough", tomorrow))

    def test_vitals_screen_accepts_fahrenheit_and_keeps_input_on_error(self):
        url = reverse("clinical:vitals_add", args=[self.enc.pk])
        r = self.client.post(url, {"temperature": "101.2", "temperature_unit": "F", "systolic": "130", "diastolic": "85"})
        self.assertEqual(r.status_code, 302)
        v = self.enc.vitals.get()
        self.assertEqual(str(v.temperature_c), "38.4")
        bad = self.client.post(url, {"systolic": "80", "diastolic": "120", "temperature_unit": "F", "pulse": "77"})
        self.assertContains(bad, "Systolic pressure must be higher")
        self.assertContains(bad, 'value="77"')  # what the doctor typed is not lost
        self.assertEqual(self.enc.vitals.count(), 1)

    def test_void_vital_needs_reason(self):
        self.client.post(reverse("clinical:vitals_add", args=[self.enc.pk]), {"pulse": "70", "temperature_unit": "F"})
        v = self.enc.vitals.get()
        url = reverse("clinical:vitals_void", args=[self.enc.pk, v.pk])
        self.client.post(url, {"reason": ""})
        v.refresh_from_db()
        self.assertFalse(v.is_void)
        self.client.post(url, {"reason": "Wrong patient"})
        v.refresh_from_db()
        self.assertTrue(v.is_void)

    def test_record_status_view_scopes_to_this_patient(self):
        a = services.add_allergy(actor=self.w.doctor, request=None, patient=self.w.patient, allergen="Dust")
        other = make_patient(self.w.reception, name="Meena Devi", phone="9000000002")
        b = services.add_allergy(actor=self.w.doctor, request=None, patient=other, allergen="Pollen")
        self.client.post(reverse("clinical:record_status", args=[self.enc.pk, "allergy", a.pk]), {"status": "ERROR"})
        a.refresh_from_db()
        self.assertEqual(a.status, RecordStatus.ENTERED_IN_ERROR)
        r = self.client.post(reverse("clinical:record_status", args=[self.enc.pk, "allergy", b.pk]), {"status": "ERROR"})
        self.assertEqual(r.status_code, 404)

    def test_history_and_current_medicine_and_referral_and_note(self):
        e = self.enc.pk
        self.client.post(reverse("clinical:history_add", args=[e]), {"kind": "CONDITION", "description": "Diabetes", "since": "2015"})
        self.client.post(reverse("clinical:medicine_add", args=[e]), {"name": "Metformin", "dose_notes": "twice daily"})
        self.client.post(reverse("clinical:referral_add", args=[e]), {"referred_to": "District Hospital", "specialty": "Medicine", "reason": "Review"})
        self.client.post(reverse("clinical:note_add", args=[e]), {"text": "Extra observation"})
        r = self.client.get(self.url)
        for text in ("Diabetes", "Metformin", "District Hospital", "Extra observation"):
            self.assertContains(r, text)


class PrescriptionScreenTests(ViewBase):
    def setUp(self):
        super().setUp()
        self.enc = self.w.encounter()
        self.med = make_medicine()
        self.item_post = {"medicine": self.med.pk, "medicine_name": "", "strength": "", "dosage_form": "", "dose": "1 tablet",
                          "route": "Oral", "frequency": "1-0-1", "duration": "5 days", "quantity": "10", "instructions": "After food"}

    def add_item(self, **over):
        return self.client.post(reverse("clinical:rx_item_add", args=[self.enc.pk]), {**self.item_post, **over})

    def test_add_edit_remove_issue_and_print(self):
        self.assertEqual(self.add_item().status_code, 302)
        rx = Prescription.objects.get()
        item = rx.items.get()
        self.assertEqual((item.medicine, item.medicine_name, item.strength), (self.med, "TestBrand", "250 mg"))

        edit = reverse("clinical:rx_item_edit", args=[self.enc.pk, item.pk])
        self.assertEqual(self.client.get(edit).status_code, 200)
        self.client.post(edit, {**self.item_post, "dose": "2 tablets", "medicine_name": "TestBrand"})
        item.refresh_from_db()
        self.assertEqual(item.dose, "2 tablets")

        self.assertContains(self.client.get(reverse("clinical:rx_finalize", args=[rx.pk])), "Issue prescription")
        self.client.post(reverse("clinical:rx_finalize", args=[rx.pk]))
        rx.refresh_from_db()
        self.assertEqual(rx.status, "FINAL")

        page = self.client.get(reverse("clinical:rx_print", args=[rx.pk]))
        for text in (rx.number, "Dr TEST Doctor", "TEST-REG-0001", "TestBrand", "Anbu Selvan", "Savithiri Clinic", "2 tablets"):
            self.assertContains(page, text)
        self.assertNotContains(page, "SUPERSEDED")

    def test_zero_quantity_is_refused_on_screen(self):
        r = self.add_item(quantity="0")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(Prescription.objects.count(), 0)

    def test_draft_cannot_be_printed(self):
        self.add_item()
        rx = Prescription.objects.get()
        r = self.client.get(reverse("clinical:rx_print", args=[rx.pk]))
        self.assertEqual(r.status_code, 302)
        self.assertIn("cannot be printed", msgs(r)[-1])

    def test_alert_screen_requires_ticking_the_box(self):
        services.add_allergy(actor=self.w.doctor, request=None, patient=self.w.patient, allergen="TestBrand")
        self.add_item()
        rx = Prescription.objects.get()
        url = reverse("clinical:rx_finalize", args=[rx.pk])
        self.assertContains(self.client.get(url), "Allergy alerts")
        r = self.client.post(url)
        self.assertEqual(r.status_code, 200)
        rx.refresh_from_db()
        self.assertEqual(rx.status, "DRAFT")
        self.client.post(url, {"acknowledge": "on"})
        rx.refresh_from_db()
        self.assertEqual(rx.status, "FINAL")

    def test_inactive_catalogue_medicine_is_not_selectable(self):
        self.med.is_active = False
        self.med.save()
        self.assertEqual(self.add_item().status_code, 200)
        self.assertEqual(Prescription.objects.count(), 0)

    def test_correction_and_superseded_print_banner(self):
        self.add_item()
        rx = Prescription.objects.get()
        self.client.post(reverse("clinical:rx_finalize", args=[rx.pk]))
        self.client.post(reverse("clinical:rx_correct", args=[rx.pk]), {"reason": "Wrong dose"})
        draft = Prescription.objects.get(status="DRAFT")
        self.client.post(reverse("clinical:rx_finalize", args=[draft.pk]))
        self.assertContains(self.client.get(reverse("clinical:rx_print", args=[rx.pk])), "SUPERSEDED")
        self.assertContains(self.client.get(reverse("clinical:rx_print", args=[draft.pk])), "(v2)")
        self.client.post(reverse("clinical:rx_cancel", args=[draft.pk]), {"reason": "Issued in error"})
        self.assertContains(self.client.get(reverse("clinical:rx_print", args=[draft.pk])), "CANCELLED")

    def test_issued_prescription_has_no_edit_buttons_on_the_page(self):
        self.add_item()
        rx = Prescription.objects.get()
        self.client.post(reverse("clinical:rx_finalize", args=[rx.pk]))
        page = self.client.get(reverse("clinical:encounter", args=[self.enc.pk]))
        self.assertNotContains(page, reverse("clinical:rx_item_add", args=[self.enc.pk]))
        self.assertContains(page, "Print prescription")


class CompletionScreenTests(ViewBase):
    def test_complete_then_read_only_then_reopen(self):
        enc = self.w.encounter()
        e = enc.pk
        self.client.post(reverse("clinical:notes_save", args=[e]), {
            "presenting_complaints": "Headache", "symptom_duration": "", "examination_findings": "", "assessment": "Tension headache",
            "treatment_plan": "", "advice": "", "follow_up_date": "", "follow_up_notes": ""})
        r = self.client.post(reverse("clinical:complete", args=[e]))
        self.assertRedirects(r, reverse("clinical:dashboard"), fetch_redirect_response=False)
        page = self.client.get(reverse("clinical:encounter", args=[e]))
        self.assertContains(page, "read-only")
        self.assertContains(page, "Add addendum")
        self.assertNotContains(page, "Save notes")
        self.assertEqual(self.client.post(reverse("clinical:notes_save", args=[e]), {"presenting_complaints": "x"}).status_code, 200)
        self.assertEqual(Encounter.objects.get(pk=e).presenting_complaints, "Headache")

        reopen = reverse("clinical:reopen", args=[e])
        self.assertEqual(self.client.get(reopen).status_code, 200)
        self.client.post(reopen, {"reason": ""})
        self.assertEqual(Encounter.objects.get(pk=e).status, "COMPLETED")
        self.client.post(reopen, {"reason": "Add missed detail"})
        self.assertEqual(Encounter.objects.get(pk=e).status, "IN_PROGRESS")
        self.assertContains(self.client.get(reverse("clinical:encounter", args=[e])), "Save notes")

    def test_completion_error_is_shown_on_the_page(self):
        enc = self.w.encounter()
        r = self.client.post(reverse("clinical:complete", args=[enc.pk]))
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "Record the presenting complaints")


class DashboardAndQueueTests(ViewBase):
    def test_dashboard_start_continue_and_follow_ups(self):
        token = self.w.token()
        page = self.client.get(reverse("clinical:dashboard"))
        self.assertContains(page, "Anbu Selvan")
        self.assertContains(page, "Start consultation")
        r = self.client.post(reverse("clinical:start", args=[token.pk]))
        enc = Encounter.objects.get()
        self.assertRedirects(r, reverse("clinical:encounter", args=[enc.pk]), fetch_redirect_response=False)
        board = self.client.get(reverse("queueing:queue_partial"))
        self.assertContains(board, "Continue consultation")
        self.assertNotContains(board, "Mark completed")

        self.w.fill_notes(enc, follow_up_date=timezone.localdate(), follow_up_notes="Review BP")
        services.complete_consultation(actor=self.w.doctor, request=None, encounter_id=enc.pk)
        page = self.client.get(reverse("clinical:dashboard"))
        self.assertContains(page, "Review BP")
        self.assertContains(page, "Recently completed")

    def test_starting_a_cancelled_token_shows_a_message(self):
        from apps.queueing import services as qs
        token = self.w.token()
        qs.change_token_status(actor=self.w.reception, request=None, token_id=token.pk, action="cancel", reason="left")
        r = self.client.post(reverse("clinical:start", args=[token.pk]))
        self.assertIn("finished or cancelled", msgs(r)[-1])
        self.assertEqual(Encounter.objects.count(), 0)

    def test_clinical_record_page_lists_visits_and_link_visible_only_to_doctor(self):
        enc = self.w.encounter()
        self.w.fill_notes(enc)
        page = self.client.get(reverse("clinical:record", args=[self.w.patient.pk]))
        self.assertContains(page, "Visits")
        self.assertContains(self.client.get(reverse("patients:detail", args=[self.w.patient.pk])), "Clinical record")
        self.client.force_login(self.w.reception)
        self.assertNotContains(self.client.get(reverse("patients:detail", args=[self.w.patient.pk])), "Clinical record")


class AdminSiteTests(TestCase):
    def test_clinical_data_cannot_be_added_changed_or_deleted_in_django_admin(self):
        from apps.accounts.tests.helpers import make_user
        admin = make_user("boss", "admin")
        self.client.force_login(admin)
        for name in ("encounter", "prescription", "clinicalnote", "encounterrevision", "allergy"):
            self.assertEqual(self.client.get(reverse(f"admin:clinical_{name}_add")).status_code, 403, name)


class DraftBannerAndFrequencyTests(ViewBase):
    def setUp(self):
        super().setUp()
        self.enc = self.w.encounter()
        self.url = reverse("clinical:encounter", args=[self.enc.pk])

    def test_banner_appears_only_while_a_draft_is_open_and_links_to_it(self):
        self.assertNotContains(self.client.get(self.url), "A draft prescription is open")
        self.w.add_item(self.enc)
        page = self.client.get(self.url)
        self.assertContains(page, "A draft prescription is open")
        self.assertContains(page, "1 medicine so far")
        self.assertContains(page, 'href="#prescription"')
        self.assertContains(page, 'id="prescription"')
        self.w.add_item(self.enc, medicine_name="Second")
        self.assertContains(self.client.get(self.url), "2 medicines so far")
        rx = Prescription.objects.get()
        services.finalize_prescription(actor=self.w.doctor, request=None, prescription_id=rx.pk)
        self.assertNotContains(self.client.get(self.url), "A draft prescription is open")

    def test_banner_is_labelled_for_a_correction_draft(self):
        rx = self.w.issue(self.enc)
        services.start_correction(actor=self.w.doctor, request=None, prescription_id=rx.pk, reason="Dose")
        self.assertContains(self.client.get(self.url), "A draft prescription is open (correction)")

    def test_four_times_a_day_pattern_is_suggested_on_both_prescription_screens(self):
        self.assertContains(self.client.get(self.url), '<option value="1-1-1-1">')
        item = self.w.add_item(self.enc)
        edit = reverse("clinical:rx_item_edit", args=[self.enc.pk, item.pk])
        self.assertContains(self.client.get(edit), '<option value="1-1-1-1">')

    def test_frequency_is_free_text_so_1_1_1_1_is_accepted_on_save(self):
        r = self.client.post(reverse("clinical:rx_item_add", args=[self.enc.pk]), {
            "medicine": "", "medicine_name": "Test Tablet", "strength": "", "dosage_form": "", "dose": "1 tablet",
            "route": "Oral", "frequency": "1-1-1-1", "duration": "3 days", "quantity": "12", "instructions": ""})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(Prescription.objects.get().items.get().frequency, "1-1-1-1")
