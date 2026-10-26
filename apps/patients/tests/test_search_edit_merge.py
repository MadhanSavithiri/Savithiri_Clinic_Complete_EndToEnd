from datetime import date

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from apps.accounts.tests.helpers import make_user
from apps.core.models import AuditEvent
from apps.patients import services
from apps.patients.models import Patient, PatientIdentifier, PatientMergeLog
from apps.queueing import services as qs
from apps.queueing.models import Appointment, QueueToken

from .helpers import make_patient, registration_post


class SearchTests(TestCase):
    def setUp(self):
        self.reception = make_user("rita", "reception")
        self.client.force_login(self.reception)
        self.a = make_patient(self.reception, name="Ramesh Kumar", phone="9876543210")
        self.b = make_patient(self.reception, name="Meena Devi", phone="9444455555", sex="F", dob=date(1990, 1, 1))
        PatientIdentifier.objects.create(patient=self.b, id_type="ABHA", value="12-3456-7890-1234", created_by=self.reception)

    def ids(self, q):
        return {p.patient_id for p in services.search_patients(q)}

    def test_by_patient_id_and_number(self):
        self.assertEqual(self.ids("P-000001"), {"P-000001"})
        self.assertEqual(self.ids("p-000002"), {"P-000002"})
        self.assertEqual(self.ids("2"), {"P-000002"})

    def test_by_name_words_any_order_and_partial(self):
        self.assertEqual(self.ids("kumar ramesh"), {"P-000001"})
        self.assertEqual(self.ids("mee"), {"P-000002"})

    def test_by_phone_digits(self):
        self.assertEqual(self.ids("98765"), {"P-000001"})
        self.assertEqual(self.ids("55555"), {"P-000002"})

    def test_by_other_identifier(self):
        self.assertEqual(self.ids("12-3456-7890-1234"), {"P-000002"})

    def test_no_match_and_empty(self):
        self.assertEqual(self.ids("zzzz"), set())
        self.assertEqual(self.ids(""), {"P-000001", "P-000002"})

    def test_merged_records_hidden(self):
        Patient.objects.filter(pk=self.a.pk).update(is_active=False, merged_into=self.b)
        self.assertEqual(self.ids("ramesh"), set())

    def test_page_and_live_partial(self):
        self.assertContains(self.client.get(reverse("patients:list"), {"q": "meena"}), "Meena Devi")
        r = self.client.get(reverse("patients:search_partial"), {"q": "meena"}, HTTP_X_LIVE_POLL="1")
        self.assertContains(r, "Meena Devi")
        self.assertNotContains(r, "Ramesh")

    def test_partial_without_login_is_401_not_a_login_page(self):
        self.client.logout()
        self.assertEqual(self.client.get(reverse("patients:search_partial")).status_code, 401)

    def test_roles_without_patient_view_are_refused(self):
        self.client.force_login(make_user("pharm", "pharmacy"))
        self.assertEqual(self.client.get(reverse("patients:list")).status_code, 403)
        self.assertEqual(self.client.get(reverse("patients:search_partial")).status_code, 403)


class DetailAndEditTests(TestCase):
    def setUp(self):
        self.reception = make_user("rita", "reception")
        self.p = make_patient(self.reception)
        self.url = reverse("patients:edit", args=[self.p.pk])

    def test_viewing_a_record_is_audited(self):
        self.client.force_login(self.reception)
        self.assertEqual(self.client.get(reverse("patients:detail", args=[self.p.pk])).status_code, 200)
        self.assertTrue(AuditEvent.objects.filter(action="patient.view", username="rita", object_id=str(self.p.pk)).exists())

    def test_doctor_can_view_but_not_edit_or_register(self):
        self.client.force_login(make_user("doc", "doctor"))
        self.assertEqual(self.client.get(reverse("patients:detail", args=[self.p.pk])).status_code, 200)
        self.assertEqual(self.client.get(self.url).status_code, 403)

    def test_pharmacy_cannot_open_patient_record(self):
        self.client.force_login(make_user("pharm", "pharmacy"))
        self.assertEqual(self.client.get(reverse("patients:detail", args=[self.p.pk])).status_code, 403)

    def test_edit_logs_field_names_but_not_values(self):
        self.client.force_login(self.reception)
        r = self.client.post(self.url, registration_post(phone="9000011111", address="New Street 12"))
        self.assertEqual(r.status_code, 302)
        self.p.refresh_from_db()
        self.assertEqual((self.p.phone, self.p.address), ("9000011111", "New Street 12"))
        ev = AuditEvent.objects.get(action="patient.update")
        self.assertIn("phone", ev.summary)
        self.assertIn("address", ev.summary)
        self.assertNotIn("9000011111", ev.summary + str(ev.changes))
        self.assertNotIn("New Street", ev.summary + str(ev.changes))

    def test_unchanged_edit_writes_no_audit_event(self):
        self.client.force_login(self.reception)
        self.client.post(self.url, registration_post(consent_given=None, consent_given_by="", phone="9876543210", address=""))
        self.assertFalse(AuditEvent.objects.filter(action="patient.update").exists())

    def test_withdrawing_consent_is_audited(self):
        self.client.force_login(self.reception)
        self.client.post(self.url, registration_post(consent_given="on", consent_given_by="Self", address=""))
        self.client.post(self.url, registration_post(consent_given=None, consent_given_by="Self", address=""))
        self.assertTrue(AuditEvent.objects.filter(action="patient.consent_withdrawn").exists())

    def test_add_identifier_and_duplicate_identifier_refused(self):
        self.client.force_login(self.reception)
        url = reverse("patients:identifier_add", args=[self.p.pk])
        self.client.post(url, {"id_type": "OLD_RECORD", "value": " a-17 "})
        self.assertEqual(PatientIdentifier.objects.get().value, "A-17")
        other = make_patient(self.reception, name="Other Person", phone="9555500000")
        self.client.post(reverse("patients:identifier_add", args=[other.pk]), {"id_type": "OLD_RECORD", "value": "A-17"})
        self.assertEqual(PatientIdentifier.objects.count(), 1)


class MergeTests(TestCase):
    def setUp(self):
        self.admin = make_user("boss", "admin")
        self.reception = make_user("rita", "reception")
        self.keep = make_patient(self.reception, name="Ramesh Kumar", phone="")
        self.dupe = make_patient(self.reception, name="Ramesh Kumar", phone="9876543210", address="Lalgudi")
        PatientIdentifier.objects.create(patient=self.dupe, id_type="OLD_RECORD", value="X-1", created_by=self.reception)

    def merge(self):
        return services.merge_patients(actor=self.admin, request=None, target=self.keep, source=self.dupe, reason="Same person")

    def test_merge_moves_records_fills_blanks_and_marks_source(self):
        qs.register_walk_in(actor=self.reception, request=None, patient=self.dupe)
        qs.book_appointment(actor=self.reception, request=None, patient=self.dupe, date=timezone.localdate())
        self.merge()
        self.keep.refresh_from_db()
        self.dupe.refresh_from_db()
        self.assertFalse(self.dupe.is_active)
        self.assertEqual(self.dupe.merged_into, self.keep)
        self.assertEqual(self.keep.phone, "9876543210")
        self.assertEqual(self.keep.address, "Lalgudi")
        self.assertEqual(QueueToken.objects.filter(patient=self.keep).count(), 1)
        self.assertEqual(QueueToken.objects.filter(patient=self.dupe).count(), 0)
        self.assertEqual(Appointment.objects.filter(patient=self.keep).count(), 2)
        self.assertEqual(self.keep.identifiers.get().value, "X-1")
        log = PatientMergeLog.objects.get()
        self.assertEqual(log.moved_counts["queueing.QueueToken"], 1)
        ev = AuditEvent.objects.get(action="patient.merge")
        self.assertNotIn("Ramesh", ev.summary)
        self.assertIn(self.dupe.patient_id, ev.summary)

    def test_target_data_is_never_overwritten(self):
        Patient.objects.filter(pk=self.keep.pk).update(address="Keep Street")
        self.merge()
        self.keep.refresh_from_db()
        self.assertEqual(self.keep.address, "Keep Street")

    def test_cannot_merge_into_itself_or_twice(self):
        with self.assertRaises(services.PatientError):
            services.merge_patients(actor=self.admin, request=None, target=self.keep, source=self.keep, reason="x")
        self.merge()
        with self.assertRaises(services.PatientError):
            self.merge()

    def test_two_open_tokens_today_blocks_merge_and_changes_nothing(self):
        qs.register_walk_in(actor=self.reception, request=None, patient=self.keep)
        qs.register_walk_in(actor=self.reception, request=None, patient=self.dupe)
        with self.assertRaises(services.PatientError):
            self.merge()
        self.dupe.refresh_from_db()
        self.assertTrue(self.dupe.is_active)
        self.assertEqual(QueueToken.objects.filter(patient=self.dupe).count(), 1)
        self.assertEqual(PatientMergeLog.objects.count(), 0)

    def test_merge_screens_admin_only(self):
        url = reverse("patients:merge", args=[self.keep.pk])
        for role in ("reception", "doctor", "pharmacy"):
            self.client.force_login(make_user(f"u_{role}", role))
            self.assertEqual(self.client.get(url).status_code, 403, role)
        self.client.force_login(self.admin)
        self.assertEqual(self.client.get(url).status_code, 200)

    def test_merge_through_the_screens(self):
        self.client.force_login(self.admin)
        r = self.client.post(reverse("patients:merge", args=[self.keep.pk]), {"other_patient_id": self.dupe.patient_id.lower()})
        confirm = reverse("patients:merge_confirm", args=[self.keep.pk, self.dupe.pk])
        self.assertRedirects(r, confirm, fetch_redirect_response=False)
        self.assertContains(self.client.get(confirm), "KEEP")
        # must tick the confirmation box
        self.client.post(confirm, {"reason": "Same person"})
        self.dupe.refresh_from_db()
        self.assertTrue(self.dupe.is_active)
        self.client.post(confirm, {"reason": "Same person", "confirm": "on"})
        self.dupe.refresh_from_db()
        self.assertFalse(self.dupe.is_active)

    def test_unknown_id_rejected(self):
        self.client.force_login(self.admin)
        r = self.client.post(reverse("patients:merge", args=[self.keep.pk]), {"other_patient_id": "P-999999"})
        self.assertContains(r, "No current patient has that ID")
