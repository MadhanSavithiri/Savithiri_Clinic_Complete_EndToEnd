from datetime import date

from django.test import TestCase
from django.urls import reverse

from apps.accounts.tests.helpers import make_user
from apps.core.models import AuditEvent
from apps.patients.models import Patient

from .helpers import make_patient, registration_post


class RegistrationTests(TestCase):
    def setUp(self):
        self.reception = make_user("rita", "reception")
        self.client.force_login(self.reception)
        self.url = reverse("patients:create")

    def test_register_creates_patient_with_automatic_id(self):
        r = self.client.post(self.url, registration_post())
        p = Patient.objects.get()
        self.assertRedirects(r, reverse("patients:detail", args=[p.pk]), fetch_redirect_response=False)
        self.assertEqual((p.patient_id, p.serial), ("P-000001", 1))
        self.assertEqual(p.phone, "9876543210")  # spaces removed
        self.assertEqual(p.created_by, self.reception)
        self.assertFalse(p.dob_estimated)

    def test_ids_increase(self):
        self.client.post(self.url, registration_post(full_name="Anbu Selvan", phone="9000000001"))
        self.client.post(self.url, registration_post(full_name="Meena Devi", phone="9000000002", sex="F"))
        self.assertEqual(list(Patient.objects.order_by("serial").values_list("patient_id", flat=True)), ["P-000001", "P-000002"])

    def test_patient_without_phone_can_register(self):
        self.client.post(self.url, registration_post(phone=""))
        self.assertEqual(Patient.objects.get().phone, "")

    def test_age_only_creates_estimated_dob(self):
        self.client.post(self.url, registration_post(date_of_birth="", age_years="40"))
        p = Patient.objects.get()
        self.assertTrue(p.dob_estimated)
        self.assertEqual(p.date_of_birth.year, date.today().year - 40)
        self.assertTrue(p.age_display().startswith("~40"))

    def test_dob_or_age_required(self):
        r = self.client.post(self.url, registration_post(date_of_birth="", age_years=""))
        self.assertContains(r, "Enter the date of birth or the age.")
        self.assertEqual(Patient.objects.count(), 0)

    def test_future_dob_rejected(self):
        r = self.client.post(self.url, registration_post(date_of_birth="2999-01-01"))
        self.assertContains(r, "cannot be in the future")
        self.assertEqual(Patient.objects.count(), 0)

    def test_bad_phone_rejected(self):
        r = self.client.post(self.url, registration_post(phone="12345"))
        self.assertContains(r, "10-digit")
        self.assertEqual(Patient.objects.count(), 0)

    def test_phone_with_country_code_is_normalised(self):
        self.client.post(self.url, registration_post(phone="+91 98765-43210"))
        self.assertEqual(Patient.objects.get().phone, "9876543210")

    def test_consent_records_who_and_when(self):
        self.client.post(self.url, registration_post())
        p = Patient.objects.get()
        self.assertTrue(p.consent_given)
        self.assertEqual(p.consent_recorded_by, self.reception)
        self.assertIsNotNone(p.consent_recorded_at)

    def test_consent_needs_a_name_when_ticked(self):
        r = self.client.post(self.url, registration_post(consent_given_by=""))
        self.assertContains(r, "Say who gave the consent")

    def test_registration_without_consent_is_allowed_but_flagged(self):
        self.client.post(self.url, registration_post(consent_given=None, consent_given_by=""))
        p = Patient.objects.get()
        self.assertFalse(p.consent_given)
        self.assertContains(self.client.get(reverse("patients:detail", args=[p.pk])), "Not recorded")

    def test_audit_event_has_no_personal_data(self):
        self.client.post(self.url, registration_post())
        ev = AuditEvent.objects.get(action="patient.create")
        blob = f"{ev.summary} {ev.object_repr} {ev.changes} {ev.username}"
        self.assertNotIn("Ramesh", blob)
        self.assertNotIn("9876543210", blob)
        self.assertIn("P-000001", blob)

    def test_only_reception_can_register(self):
        for role in ("doctor", "pharmacy", "admin"):
            self.client.force_login(make_user(f"u_{role}", role))
            self.assertEqual(self.client.get(self.url).status_code, 403, role)
            self.assertEqual(self.client.post(self.url, registration_post()).status_code, 403, role)
        self.assertEqual(Patient.objects.count(), 0)

    def test_anonymous_redirected(self):
        self.client.logout()
        self.assertEqual(self.client.get(self.url).status_code, 302)


class DuplicateTests(TestCase):
    def setUp(self):
        self.reception = make_user("rita", "reception")
        self.client.force_login(self.reception)
        self.url = reverse("patients:create")
        self.first = make_patient(self.reception)

    def test_same_name_and_dob_is_flagged_and_not_saved(self):
        r = self.client.post(self.url, registration_post(phone="9111111111"))
        self.assertContains(r, "may already be registered")
        self.assertContains(r, self.first.patient_id)
        self.assertEqual(Patient.objects.count(), 1)

    def test_spacing_case_and_dots_do_not_hide_a_duplicate(self):
        r = self.client.post(self.url, registration_post(full_name="  RAMESH   kumar. "))
        self.assertContains(r, "may already be registered")

    def test_staff_can_confirm_it_is_a_different_person(self):
        data = registration_post()
        data["confirm_new"] = "1"
        self.client.post(self.url, data)
        self.assertEqual(Patient.objects.count(), 2)

    def test_same_name_different_dob_is_not_flagged(self):
        r = self.client.post(self.url, registration_post(date_of_birth="1990-01-01", phone="9222222222"))
        self.assertEqual(r.status_code, 302)
        self.assertEqual(Patient.objects.count(), 2)

    def test_family_sharing_a_phone_is_not_flagged(self):
        r = self.client.post(self.url, registration_post(full_name="Lakshmi Kumar", sex="F", date_of_birth="1988-05-05"))
        self.assertEqual(r.status_code, 302)
        self.assertEqual(Patient.objects.count(), 2)

    def test_same_phone_and_first_name_is_flagged(self):
        r = self.client.post(self.url, registration_post(full_name="Ramesh Babu", date_of_birth="1960-01-01"))
        self.assertContains(r, "may already be registered")

    def test_merged_records_are_not_offered_as_duplicates(self):
        Patient.objects.filter(pk=self.first.pk).update(is_active=False, merged_into=make_patient(
            self.reception, name="Someone Else", phone="9333333333"))
        r = self.client.post(self.url, registration_post())
        self.assertEqual(r.status_code, 302)


class ModelTests(TestCase):
    def test_age_display(self):
        u = make_user("rita", "reception")
        p = make_patient(u, dob=date(2024, 10, 19))
        self.assertEqual(p.age_display(today=date(2026, 10, 19)), "2 y")
        p.date_of_birth = date(2025, 4, 1)
        self.assertEqual(p.age_display(today=date(2026, 10, 19)), "18 m")
        p.dob_estimated = True
        self.assertEqual(p.age_display(today=date(2026, 10, 19)), "~18 m")
