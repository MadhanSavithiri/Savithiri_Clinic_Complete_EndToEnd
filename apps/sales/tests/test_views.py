from datetime import timedelta
from decimal import Decimal

from django.contrib.messages import get_messages
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from apps.accounts.tests.helpers import PW, make_user
from apps.core.models import AuditEvent
from apps.sales import services
from apps.sales.models import Sale

from .helpers import CounterShop

LOCATION = "Rack R02 \u2192 Shelf S03 \u2192 Box B12"


def msgs(r):
    return [str(m) for m in get_messages(r.wsgi_request)]


class ScreenBase(TestCase):
    def setUp(self):
        self.s = CounterShop()
        self.client.force_login(self.s.counter)

    def add(self, qty=5, med=None):
        return self.client.post(reverse("sales:cart_add"), {"medicine_id": (med or self.s.med).pk, "quantity": qty})

    def checkout(self, total="10.00", **over):
        draft = services.get_draft(self.s.counter)
        data = {"sale_id": draft.pk, "expected_subtotal": total, "discount_percent": "0", "discount_reason": "",
                "pay1_method": "CASH", "pay1_amount": total, "customer_name": "", "customer_phone": ""}
        data.update(over)
        return self.client.post(reverse("sales:checkout"), data)


class BillingScreenTests(ScreenBase):
    def test_the_counter_person_lands_on_the_billing_screen(self):
        self.assertRedirects(self.client.get("/"), reverse("sales:counter"), fetch_redirect_response=False)

    def test_without_a_shift_the_screen_asks_to_open_one(self):
        other = make_user("c2", "counter")
        self.client.force_login(other)
        page = self.client.get(reverse("sales:counter"))
        self.assertContains(page, "Open your shift")
        self.assertNotContains(page, "Find an item")
        self.client.post(reverse("sales:shift_open"), {"opening_cash": "250"})
        self.assertContains(self.client.get(reverse("sales:counter")), "Find an item")

    def test_search_shows_location_stock_batch_and_price(self):
        page = self.client.get(reverse("sales:counter"), {"q": "parac"})
        for text in (LOCATION, "Paracetamol", "PB1", "100", "Add"):
            self.assertContains(page, text)

    def test_live_search_partial(self):
        r = self.client.get(reverse("sales:search"), {"q": "B12"}, HTTP_X_LIVE_POLL="1")
        self.assertContains(r, LOCATION)
        self.client.logout()
        self.assertEqual(self.client.get(reverse("sales:search"), {"q": "x"}).status_code, 401)
        self.client.force_login(self.s.doctor)
        self.assertEqual(self.client.get(reverse("sales:search"), {"q": "x"}).status_code, 403)

    def test_cart_add_change_remove_and_discard(self):
        self.assertEqual(self.add(5).status_code, 302)
        page = self.client.get(reverse("sales:counter"))
        self.assertContains(page, "Open bill")
        self.assertContains(page, LOCATION)  # the location is also shown in the cart
        line = services.get_draft(self.s.counter).lines.get()
        self.client.post(reverse("sales:cart_quantity", args=[line.pk]), {"quantity": "7"})
        line.refresh_from_db()
        self.assertEqual(line.quantity, 7)
        r = self.add(500)
        self.assertIn("Only", msgs(r)[-1])
        self.client.post(reverse("sales:cart_remove", args=[line.pk]))
        self.assertEqual(services.get_draft(self.s.counter).lines.count(), 0)
        self.add(1)
        self.client.post(reverse("sales:cart_discard"))
        self.assertIsNone(services.get_draft(self.s.counter))

    def test_completing_a_sale_shows_the_receipt(self):
        self.add(5)
        r = self.checkout("10.00", customer_name="Meena")
        sale = Sale.objects.get(status="COMPLETED")
        self.assertRedirects(r, reverse("sales:receipt", args=[sale.pk]), fetch_redirect_response=False)
        page = self.client.get(reverse("sales:receipt", args=[sale.pk]))
        for text in ("INDEPENDENT COUNTER SALE", sale.number, "Paracetamol 500 mg Tablet", "Batch PB1", "10.00",
                     "Paid by Cash", "Meena", "Prices are MRP"):
            self.assertContains(page, text)
        self.assertNotContains(page, "DUPLICATE COPY")
        self.assertNotContains(page, "consultation")

    def test_reprints_say_duplicate_and_are_audited_and_a4_is_available(self):
        self.add(1)
        self.checkout("2.00")
        sale = Sale.objects.get()
        url = reverse("sales:receipt", args=[sale.pk])
        self.client.get(url)
        again = self.client.get(url)
        self.assertContains(again, "DUPLICATE COPY")
        self.assertTrue(AuditEvent.objects.filter(action="sale.receipt_reprint", username="counter1").exists())
        self.assertContains(self.client.get(url, {"size": "a4"}), "size: A4")

    def test_a_failed_checkout_keeps_what_was_typed_and_takes_nothing(self):
        self.add(5)
        r = self.checkout("10.00", pay1_amount="9.00", customer_name="Meena")
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "must add up")
        self.assertContains(r, 'value="Meena"')
        self.assertEqual(Sale.objects.filter(status="COMPLETED").count(), 0)
        self.s.stock_batch.refresh_from_db()
        self.assertEqual(self.s.stock_batch.quantity_on_hand, 100)

    def test_amount_is_required_and_bad_phone_is_refused(self):
        self.add(1)
        self.assertContains(self.checkout("2.00", pay1_amount=""), "Enter the amount received")
        self.assertContains(self.checkout("2.00", customer_phone="123"), "10-digit")
        self.assertEqual(Sale.objects.filter(status="COMPLETED").count(), 0)

    def test_pressing_complete_twice_shows_the_same_receipt(self):
        self.add(2)
        draft = services.get_draft(self.s.counter)
        first = self.checkout("4.00")
        second = self.client.post(reverse("sales:checkout"), {
            "sale_id": draft.pk, "expected_subtotal": "4.00", "discount_percent": "0", "pay1_method": "CASH", "pay1_amount": "4.00"})
        self.assertEqual(first["Location"], second["Location"])
        self.assertEqual(Sale.objects.filter(status="COMPLETED").count(), 1)
        self.s.stock_batch.refresh_from_db()
        self.assertEqual(self.s.stock_batch.quantity_on_hand, 98)


class PrescriptionOnlyScreenTests(ScreenBase):
    def setUp(self):
        super().setUp()
        self.rx = self.s.medicine("Amoxycillin", "500 mg", schedule_category="H")
        self.s.batch(self.rx, qty=20, days=200, number="RX1", sell="4")
        self.add(2, self.rx)

    def data(self, **over):
        d = {"rx_prescriber_name": "Dr Test Prescriber", "rx_prescriber_reg_no": "TEST-REG-77", "rx_patient_name": "Walk In Patient",
             "approver_username": "cpharm", "approver_password": PW}
        d.update(over)
        return d

    def test_the_screen_asks_for_prescription_and_approval(self):
        page = self.client.get(reverse("sales:counter"))
        self.assertContains(page, "Prescription-only medicine on this bill")
        self.assertContains(page, "Pharmacist password")

    def test_approval_through_the_screen_and_the_receipt_shows_it(self):
        r = self.checkout("8.00", **self.data())
        sale = Sale.objects.get(status="COMPLETED")
        self.assertRedirects(r, reverse("sales:receipt", args=[sale.pk]), fetch_redirect_response=False)
        page = self.client.get(reverse("sales:receipt", args=[sale.pk]))
        self.assertContains(page, "Dr Test Prescriber")
        self.assertContains(page, "TEST-REG-77")
        self.assertContains(page, "Approved by")

    def test_a_wrong_password_is_refused_and_never_shown_again(self):
        r = self.checkout("8.00", **self.data(approver_password="wrong-password-9"))
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "approval failed")
        self.assertNotContains(r, "wrong-password-9")
        self.assertNotContains(r, PW)
        self.assertEqual(Sale.objects.filter(status="COMPLETED").count(), 0)


class PrivacyAndAccessTests(ScreenBase):
    def test_purchase_prices_and_suppliers_never_reach_the_counter_screens_or_receipts(self):
        secret = self.s.medicine("Secretdrug", "")
        self.s.batch(secret, qty=10, days=100, number="SEC1", cost="7.7777", sell="9")
        self.add(1, secret)
        pages = [self.client.get(reverse("sales:counter")), self.client.get(reverse("sales:counter"), {"q": "secret"}),
                 self.client.get(reverse("sales:search"), {"q": "secret"})]
        self.checkout("9.00")
        sale = Sale.objects.get()
        pages += [self.client.get(reverse("sales:receipt", args=[sale.pk])), self.client.get(reverse("sales:receipt", args=[sale.pk]), {"size": "a4"}),
                  self.client.get(reverse("sales:my_sales")), self.client.get(reverse("sales:shift"))]
        for page in pages:
            self.assertEqual(page.status_code, 200)
            body = page.content.decode()
            for needle in ("7.7777", "7.78", "Test Supplier", "Cost", "Purchase", "Margin", "profit"):
                self.assertNotIn(needle, body, needle)

    def test_counter_staff_cannot_open_any_stock_cost_clinical_or_admin_screen(self):
        urls = [reverse(n) for n in ("patients:list", "clinical:dashboard", "queueing:queue", "queueing:appointment_list",
                                     "pharmacy:list", "pharmacy:create", "pharmacy:stock_overview", "pharmacy:batch_report",
                                     "pharmacy:expiry_report", "pharmacy:ledger", "pharmacy:valuation", "pharmacy:purchase_list",
                                     "pharmacy:purchase_create", "pharmacy:supplier_list", "pharmacy:purchase_report",
                                     "accounts:user_list", "sales:all_sales")]
        urls += [reverse("pharmacy:medicine_detail", args=[self.s.med.pk]), reverse("pharmacy:edit", args=[self.s.med.pk]),
                 reverse("pharmacy:batch_detail", args=[self.s.stock_batch.pk])]
        for url in urls:
            self.assertEqual(self.client.get(url).status_code, 403, url)
        for name in ("batch_adjust", "batch_wastage", "batch_return", "batch_recall"):
            r = self.client.post(reverse(f"pharmacy:{name}", args=[self.s.stock_batch.pk]), {"quantity": "5", "reason": "x", "direction": "remove"})
            self.assertEqual(r.status_code, 403, name)
        self.s.stock_batch.refresh_from_db()
        self.assertEqual(self.s.stock_batch.quantity_on_hand, 100)
        self.assertNotEqual(self.client.get("/admin/").status_code, 200)

    def test_nobody_but_counter_staff_and_the_manager_can_bill(self):
        for user, code in ((self.s.doctor, 403), (self.s.reception, 403), (self.s.clinic_pharm, 403), (self.s.admin, 403),
                           (self.s.pharm, 200)):
            self.client.force_login(user)
            self.assertEqual(self.client.get(reverse("sales:counter")).status_code, code, user.role)
        for user in (self.s.doctor, self.s.reception, self.s.clinic_pharm, self.s.admin):
            self.client.force_login(user)
            self.assertEqual(self.client.post(reverse("sales:cart_add"), {"medicine_id": self.s.med.pk, "quantity": 1}).status_code, 403)
            self.assertEqual(self.client.post(reverse("sales:checkout"), {"sale_id": 1}).status_code, 403)

    def test_receipts_are_private_to_the_cashier_the_manager_and_the_admin(self):
        self.add(1)
        self.checkout("2.00")
        url = reverse("sales:receipt", args=[Sale.objects.get().pk])
        for user, code in ((self.s.counter, 200), (self.s.pharm, 200), (self.s.admin, 200),
                           (make_user("c2", "counter"), 403), (self.s.doctor, 403), (self.s.reception, 403), (self.s.clinic_pharm, 403)):
            self.client.force_login(user)
            self.assertEqual(self.client.get(url).status_code, code, user.role)
        self.client.logout()
        self.assertEqual(self.client.get(url).status_code, 302)

    def test_counter_sales_are_not_clinic_sales(self):
        self.add(1)
        self.checkout("2.00")
        sale = Sale.objects.get()
        self.assertEqual((sale.channel, sale.patient_id, sale.prescription_id), ("INDEPENDENT_COUNTER", None, None))
        self.assertEqual(Sale.objects.filter(channel="CLINIC_PHARMACY").count(), 0)


class SalesListAndShiftScreenTests(ScreenBase):
    def sell_as(self, user, qty=1):
        self.client.force_login(user)
        self.add(qty)
        draft = services.get_draft(user)
        total = str(Decimal(qty) * Decimal("2"))
        self.client.post(reverse("sales:checkout"), {"sale_id": draft.pk, "expected_subtotal": total, "discount_percent": "0",
                                                      "pay1_method": "CASH", "pay1_amount": total})

    def test_my_sales_shows_only_my_own_bills(self):
        other = make_user("c2", "counter")
        self.s.open_shift_for(other)
        self.sell_as(self.s.counter)
        self.sell_as(other)
        first, second = Sale.objects.order_by("id")
        self.client.force_login(self.s.counter)
        page = self.client.get(reverse("sales:my_sales"))
        self.assertContains(page, first.number)
        self.assertNotContains(page, second.number)

    def test_manager_and_admin_see_everything_and_can_filter_by_cashier_and_date(self):
        other = make_user("c2", "counter")
        self.s.open_shift_for(other)
        self.sell_as(self.s.counter)
        self.sell_as(other)
        first, second = Sale.objects.order_by("id")
        for user in (self.s.pharm, self.s.admin):
            self.client.force_login(user)
            page = self.client.get(reverse("sales:all_sales"))
            self.assertContains(page, first.number)
            self.assertContains(page, second.number)
        only = self.client.get(reverse("sales:all_sales"), {"cashier": other.pk})
        self.assertContains(only, second.number)
        self.assertNotContains(only, first.number)
        tomorrow = (timezone.localdate() + timedelta(days=1)).isoformat()
        self.assertContains(self.client.get(reverse("sales:all_sales"), {"from": tomorrow, "to": tomorrow}), "No sales in this period")
        self.client.force_login(self.s.counter)
        self.assertEqual(self.client.get(reverse("sales:all_sales")).status_code, 403)

    def test_shift_page_close_and_reopen(self):
        self.add(5)
        self.checkout("10.00")
        page = self.client.get(reverse("sales:shift"))
        self.assertContains(page, "Cash that should be in the drawer")
        self.assertContains(page, "510.00")
        r = self.client.post(reverse("sales:shift_close"), {"counted_cash": "508", "notes": "short by 2"})
        self.assertIn("difference", msgs(r)[-1])
        self.assertContains(self.client.get(reverse("sales:counter")), "Open your shift")
        r = self.client.post(reverse("sales:shift_open"), {"opening_cash": "508"})
        self.assertRedirects(r, reverse("sales:counter"), fetch_redirect_response=False)

    def test_shift_cannot_be_closed_with_items_in_the_open_bill(self):
        self.add(1)
        r = self.client.post(reverse("sales:shift_close"), {"counted_cash": "500"})
        self.assertIn("open bill", msgs(r)[-1])
        self.assertContains(self.client.get(reverse("sales:shift")), "Close the shift")
