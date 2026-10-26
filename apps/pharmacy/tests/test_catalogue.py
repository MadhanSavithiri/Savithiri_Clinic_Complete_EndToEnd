from django.test import TestCase
from django.urls import reverse

from apps.accounts.tests.helpers import make_user
from apps.core.models import AuditEvent
from apps.pharmacy.models import Medicine

DATA = {"brand_name": "TestBrand", "generic_name": "testgeneric", "strength": "500 mg", "dosage_form": "Tablet",
        "schedule_category": "NONE", "notes": "", "is_active": "on",
        "unit": "tablet", "pack_size": "10", "reorder_threshold": "0", "item_kind": "MEDICINE",
        "counter_sale_allowed": "on"}


class CatalogueTests(TestCase):
    def setUp(self):
        self.pharm = make_user("pharm", "pharmacy_manager")
        self.client.force_login(self.pharm)

    def test_catalogue_starts_empty(self):
        self.assertEqual(Medicine.objects.count(), 0)
        self.assertContains(self.client.get(reverse("pharmacy:list")), "No medicines yet")

    def test_pharmacy_can_add_and_edit_with_audit(self):
        self.client.post(reverse("pharmacy:create"), DATA)
        med = Medicine.objects.get()
        self.assertEqual((med.brand_name, med.created_by), ("TestBrand", self.pharm))
        self.client.post(reverse("pharmacy:edit", args=[med.pk]), {**DATA, "strength": "650 mg"})
        med.refresh_from_db()
        self.assertEqual(med.strength, "650 mg")
        self.assertTrue(AuditEvent.objects.filter(action="medicine.create").exists())
        ev = AuditEvent.objects.get(action="medicine.update")
        self.assertIn("strength", ev.summary)

    def test_duplicate_is_refused(self):
        self.client.post(reverse("pharmacy:create"), DATA)
        r = self.client.post(reverse("pharmacy:create"), {**DATA, "brand_name": "  TestBrand "})
        self.assertEqual(Medicine.objects.count(), 1)
        self.assertContains(r, "already exists")

    def test_search_by_generic_name(self):
        self.client.post(reverse("pharmacy:create"), DATA)
        self.assertContains(self.client.get(reverse("pharmacy:list"), {"q": "testgen"}), "TestBrand")
        self.assertNotContains(self.client.get(reverse("pharmacy:list"), {"q": "zzz"}), "TestBrand")

    def test_roles(self):
        med = Medicine.objects.create(brand_name="X", dosage_form="Tablet")
        for role, can_view, can_edit in (("doctor", True, False), ("admin", True, False),
                                          ("reception", False, False), ("pharmacy", True, False),
                                          ("pharmacy_manager", True, True), ("counter", False, False)):
            self.client.force_login(make_user(f"u_{role}", role))
            self.assertEqual(self.client.get(reverse("pharmacy:list")).status_code, 200 if can_view else 403, role)
            self.assertEqual(self.client.get(reverse("pharmacy:create")).status_code, 200 if can_edit else 403, role)
            self.assertEqual(self.client.get(reverse("pharmacy:edit", args=[med.pk])).status_code, 200 if can_edit else 403, role)

    def test_locations_are_saved_in_upper_case_and_shown_as_a_path(self):
        data = {**DATA, "rack": "r02", "shelf": "s03", "box": "b12", "item_code": "abc-1"}
        self.client.post(reverse("pharmacy:create"), data)
        med = Medicine.objects.get()
        self.assertEqual((med.rack, med.shelf, med.box, med.item_code), ("R02", "S03", "B12", "ABC-1"))
        self.assertEqual(med.location_label, "Rack R02 \u2192 Shelf S03 \u2192 Box B12")
        med.drawer = "D1"
        self.assertEqual(med.location_label, "Rack R02 \u2192 Shelf S03 \u2192 Drawer D1 \u2192 Box B12")
        self.assertContains(self.client.get(reverse("pharmacy:list")), "Rack R02")

    def test_item_code_must_be_unique_when_given_but_blank_codes_can_repeat(self):
        self.client.post(reverse("pharmacy:create"), {**DATA, "item_code": "X1"})
        r = self.client.post(reverse("pharmacy:create"), {**DATA, "brand_name": "Other", "item_code": "x1"})
        self.assertEqual(Medicine.objects.count(), 1)
        self.assertEqual(r.status_code, 200)
        self.client.post(reverse("pharmacy:create"), {**DATA, "brand_name": "B2"})
        self.client.post(reverse("pharmacy:create"), {**DATA, "brand_name": "B3"})
        self.assertEqual(Medicine.objects.count(), 3)

    def test_manager_can_download_locations_csv_but_the_clinic_pharmacist_cannot(self):
        Medicine.objects.create(brand_name="Paramol", strength="500 mg", dosage_form="Tablet", rack="R01", shelf="S02", box="B03")
        text = self.client.get(reverse("pharmacy:list"), {"fmt": "csv"}).content.decode("utf-8-sig")
        self.assertIn("rack", text.splitlines()[0])
        self.assertIn("R01", text)
        self.client.force_login(make_user("cp", "pharmacy"))
        r = self.client.get(reverse("pharmacy:list"), {"fmt": "csv"})
        self.assertNotIn("text/csv", r["Content-Type"])  # they just get the normal page
