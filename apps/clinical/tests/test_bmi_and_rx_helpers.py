import json
import re
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

from django.test import SimpleTestCase, TestCase
from django.urls import reverse

from apps.clinical import bmi, services
from apps.clinical.forms import PrescriptionItemForm
from apps.clinical.models import Prescription, VitalObservation
from apps.clinical.rxcalc import calc_quantity
from apps.core.models import Setting
from apps.patients.tests.helpers import make_patient

from .helpers import World

CASES = json.loads((Path(__file__).parent / "quantity_cases.json").read_text(encoding="utf-8"))


class QuantityCalculatorTests(SimpleTestCase):
    def test_shared_cases(self):
        # the very same cases are checked against static/js/rx_calc.js, so screen and server agree
        for c in CASES:
            self.assertEqual(calc_quantity(c["dose"], c["frequency"], c["duration"]).quantity, c["qty"], c)

    def test_explanation_shows_the_working(self):
        self.assertEqual(calc_quantity("1 tablet", "1-1-1", "5 days").explanation,
                         "3 dose(s) a day \u00d7 1 per dose \u00d7 5 day(s) = 15")

    def test_four_times_a_day_pattern(self):
        self.assertEqual(calc_quantity("1 tablet", "1-1-1-1", "5 days").quantity, 20)

    def test_rounds_up_never_down(self):
        self.assertEqual(calc_quantity("2.5 mL", "1-0-1", "3 days").quantity, 15)
        self.assertEqual(calc_quantity("\u00bd tablet", "1-0-0", "5 days").quantity, 3)  # 2.5 -> 3


class ItemFormQuantityTests(TestCase):
    base = {"dose": "1 tablet", "route": "Oral", "frequency": "1-1-1", "duration": "5 days", "medicine_name": "Test Tablet"}

    def test_blank_quantity_is_worked_out(self):
        f = PrescriptionItemForm({**self.base, "quantity": ""})
        self.assertTrue(f.is_valid(), f.errors)
        self.assertEqual(f.cleaned_data["quantity"], 15)

    def test_doctors_own_quantity_is_kept(self):
        f = PrescriptionItemForm({**self.base, "quantity": "10"})
        self.assertTrue(f.is_valid(), f.errors)
        self.assertEqual(f.cleaned_data["quantity"], 10)

    def test_when_it_cannot_be_worked_out_the_doctor_is_asked(self):
        f = PrescriptionItemForm({**self.base, "frequency": "SOS", "quantity": ""})
        self.assertFalse(f.is_valid())
        self.assertIn("cannot be counted automatically", str(f.errors["quantity"]))
        g = PrescriptionItemForm({**self.base, "duration": "", "quantity": ""})
        self.assertFalse(g.is_valid())
        self.assertIn("duration", str(g.errors["quantity"]))

    def test_zero_is_still_refused(self):
        self.assertFalse(PrescriptionItemForm({**self.base, "quantity": "0"}).is_valid())


class MealTimingTests(TestCase):
    def setUp(self):
        self.w = World()
        self.client.force_login(self.w.doctor)
        self.enc = self.w.encounter()
        self.post = {"medicine": "", "medicine_name": "Test Tablet", "strength": "", "dosage_form": "", "dose": "1 tablet",
                     "route": "Oral", "frequency": "1-0-1", "duration": "3 days", "quantity": "", "instructions": ""}

    def add(self, **over):
        return self.client.post(reverse("clinical:rx_item_add", args=[self.enc.pk]), {**self.post, **over})

    def test_form_offers_after_and_before_food_as_tick_choices(self):
        page = self.client.get(reverse("clinical:encounter", args=[self.enc.pk])).content.decode()
        self.assertEqual(self.add().status_code, 302)
        page = self.client.get(reverse("clinical:encounter", args=[self.enc.pk])).content.decode()
        self.assertIn('type="radio"', page)
        self.assertIn("After food", page)
        self.assertIn("Before food", page)
        self.assertIn("Not specified", page)

    def test_after_and_before_food_are_saved_shown_and_printed(self):
        self.add(meal_timing="AFTER")
        self.add(medicine_name="Second Tablet", meal_timing="BEFORE")
        self.add(medicine_name="Third Tablet")
        items = list(Prescription.objects.get().items.order_by("line_no"))
        self.assertEqual([i.meal_timing for i in items], ["AFTER", "BEFORE", ""])
        self.assertEqual([i.meal_text for i in items], ["After food", "Before food", ""])
        self.assertEqual(items[0].quantity, 6)  # 1-0-1 x 3 days, worked out automatically
        rx = Prescription.objects.get()
        self.assertContains(self.client.get(reverse("clinical:encounter", args=[self.enc.pk])), "After food")
        self.assertContains(self.client.get(reverse("clinical:rx_finalize", args=[rx.pk])), "Before food")
        self.client.post(reverse("clinical:rx_finalize", args=[rx.pk]))
        page = self.client.get(reverse("clinical:rx_print", args=[rx.pk]))
        self.assertContains(page, "After food")
        self.assertContains(page, "Before food")

    def test_invalid_choice_is_refused(self):
        self.assertEqual(self.add(meal_timing="WITH_WINE").status_code, 200)
        self.assertEqual(Prescription.objects.count(), 0)

    def test_editing_an_item_keeps_and_changes_the_choice(self):
        self.add(meal_timing="AFTER")
        item = Prescription.objects.get().items.get()
        url = reverse("clinical:rx_item_edit", args=[self.enc.pk, item.pk])
        html = self.client.get(url).content.decode()
        self.assertTrue(re.search(r'value="AFTER"[^>]*checked', html), "After food should be pre-selected")
        self.client.post(url, {**self.post, "meal_timing": "BEFORE", "quantity": "6"})
        item.refresh_from_db()
        self.assertEqual(item.meal_timing, "BEFORE")

    def test_a_correction_copies_the_meal_timing(self):
        self.add(meal_timing="AFTER")
        rx = Prescription.objects.get()
        services.finalize_prescription(actor=self.w.doctor, request=None, prescription_id=rx.pk)
        draft = services.start_correction(actor=self.w.doctor, request=None, prescription_id=rx.pk, reason="Dose")
        self.assertEqual(draft.items.get().meal_timing, "AFTER")


class BmiTests(TestCase):
    def test_who_categories_and_boundaries(self):
        d = Decimal
        self.assertEqual([bmi.bmi_category(d(x), "WHO") for x in ("18.4", "18.5", "24.9", "25.0", "29.9", "30.0")],
                         ["Underweight", "Normal", "Normal", "Overweight", "Overweight", "Obese"])

    def test_asian_cutoffs_are_stricter(self):
        d = Decimal
        self.assertEqual([bmi.bmi_category(d(x), "ASIAN") for x in ("22.9", "23.0", "24.9", "25.0")],
                         ["Normal", "Overweight", "Overweight", "Obese"])

    def test_the_clinic_can_choose_the_standard_in_settings(self):
        self.assertEqual(bmi.standard_key(), "WHO")
        Setting.objects.create(key="bmi_standard", value="asian")
        self.assertEqual(bmi.standard_key(), "ASIAN")
        Setting.objects.filter(key="bmi_standard").update(value="nonsense")
        self.assertEqual(bmi.standard_key(), "WHO")

    def test_adult_categories_are_not_given_to_children(self):
        self.assertEqual(bmi.bmi_text(Decimal("16"), adult=True, key="WHO"), "Underweight")
        self.assertIn("growth charts", bmi.bmi_text(Decimal("16"), adult=False))
        self.assertEqual(bmi.bmi_text(None, adult=True), "")

    def test_age_18_is_adult(self):
        class P:  # minimal stand-in
            def __init__(self, dob):
                self.date_of_birth = dob
        today = date(2026, 10, 20)
        self.assertTrue(bmi.is_adult(P(date(2008, 10, 20)), today))
        self.assertFalse(bmi.is_adult(P(date(2008, 10, 21)), today))

    def test_bmi_rounds_half_up_like_the_screen(self):
        w = World()
        enc = w.encounter()
        v = services.record_vitals(actor=w.doctor, request=None, encounter_id=enc.pk,
                                   data={"height_cm": Decimal("200"), "weight_kg": Decimal("97")})
        self.assertEqual(VitalObservation.objects.get(pk=v.pk).bmi, Decimal("24.3"))  # 24.25 exactly

    def test_page_shows_live_bmi_box_scripts_and_the_category_in_the_table(self):
        w = World()
        self.client.force_login(w.doctor)
        enc = w.encounter()
        services.record_vitals(actor=w.doctor, request=None, encounter_id=enc.pk,
                               data={"height_cm": Decimal("170"), "weight_kg": Decimal("70")})
        page = self.client.get(reverse("clinical:encounter", args=[enc.pk]))
        for text in ('id="bmi-live"', 'data-adult="1"', 'data-under="18.5"', 'data-over="25"', 'data-obese="30"',
                     "js/bmi.js", "js/rx_calc.js", "Normal"):
            self.assertContains(page, text)
        Setting.objects.create(key="bmi_standard", value="ASIAN")
        page = self.client.get(reverse("clinical:encounter", args=[enc.pk]))
        self.assertContains(page, 'data-over="23"')
        self.assertContains(page, "Overweight")  # 24.2 is overweight on the Asian scale

    def test_a_child_gets_the_value_but_no_adult_category(self):
        w = World()
        self.client.force_login(w.doctor)
        child = make_patient(w.reception, name="Little One", phone="9111111111", dob=date.today() - timedelta(days=365 * 8))
        enc = w.encounter(child)
        services.record_vitals(actor=w.doctor, request=None, encounter_id=enc.pk,
                               data={"height_cm": Decimal("120"), "weight_kg": Decimal("22")})
        page = self.client.get(reverse("clinical:encounter", args=[enc.pk]))
        self.assertContains(page, 'data-adult="0"')
        self.assertContains(page, "growth charts")
