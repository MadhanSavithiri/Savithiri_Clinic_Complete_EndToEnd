from datetime import timedelta
from decimal import Decimal
import unittest

from django.core.exceptions import PermissionDenied
from django.db import DatabaseError, IntegrityError, connection, transaction
from django.test import TestCase

from apps.accounts.tests.helpers import PW, make_user
from apps.core.models import AuditEvent, ImmutableRecordError, SalesChannel, Setting
from apps.patients.tests.helpers import make_patient
from apps.pharmacy import stock
from apps.pharmacy.models import InventoryTransaction
from apps.sales import search, services
from apps.sales.models import Payment, Sale, SaleLine, SaleLockedError

from .helpers import CounterShop

COUNTER = SalesChannel.INDEPENDENT_COUNTER


class Base(TestCase):
    def setUp(self):
        self.s = CounterShop()

    def on_hand(self, batch):
        batch.refresh_from_db()
        return batch.quantity_on_hand


class ShiftTests(Base):
    def test_billing_needs_an_open_shift(self):
        other = make_user("c2", "counter")
        with self.assertRaises(services.SaleError) as ctx:
            services.add_to_cart(actor=other, request=None, medicine_id=self.s.med.pk, quantity=1)
        self.assertIn("Open your shift", str(ctx.exception))

    def test_only_one_open_shift_per_person(self):
        with self.assertRaises(services.SaleError):
            services.open_shift(actor=self.s.counter, request=None, opening_cash=Decimal("0"))

    def test_only_sellers_open_shifts(self):
        for user in (self.s.doctor, self.s.reception, self.s.clinic_pharm, self.s.admin):
            with self.assertRaises(PermissionDenied):
                services.open_shift(actor=user, request=None, opening_cash=Decimal("0"))

    def test_summary_expected_cash_and_closing_with_a_difference(self):
        self.s.sell(5)  # 10.00 cash
        draft = self.s.cart(qty=10)
        services.complete_counter_sale(actor=self.s.counter, request=None, sale_id=draft.pk,
                                       payments=self.s.pay(Decimal("20.00"), "UPI"))
        summary = services.shift_summary(self.s.shift)
        by = {m["method"]: m for m in summary["by_method"]}
        self.assertEqual((by["CASH"]["net"], by["UPI"]["net"]), (Decimal("10.00"), Decimal("20.00")))
        self.assertEqual((summary["bills"], summary["collected"], summary["sales_total"]),
                         (2, Decimal("30.00"), Decimal("30.00")))
        self.assertEqual(summary["expected_cash"], Decimal("510.00"))  # 500 opening + 10 cash; UPI is not in the drawer
        closed = services.close_shift(actor=self.s.counter, request=None, shift_id=self.s.shift.pk, counted_cash=Decimal("505"))
        self.assertEqual((closed.expected_cash, closed.counted_cash, closed.variance),
                         (Decimal("510.00"), Decimal("505.00"), Decimal("-5.00")))
        self.assertTrue(AuditEvent.objects.filter(action="shift.close", username="counter1").exists())
        with self.assertRaises(services.SaleError):
            services.add_to_cart(actor=self.s.counter, request=None, medicine_id=self.s.med.pk, quantity=1)
        self.assertIsNotNone(services.open_shift(actor=self.s.counter, request=None, opening_cash=Decimal("505")))

    def test_cannot_close_with_items_on_the_open_bill_or_someone_elses_shift(self):
        self.s.cart(qty=1)
        with self.assertRaises(services.SaleError):
            services.close_shift(actor=self.s.counter, request=None, shift_id=self.s.shift.pk, counted_cash=Decimal("500"))
        other = make_user("c2", "counter")
        with self.assertRaises(PermissionDenied):
            services.close_shift(actor=other, request=None, shift_id=self.s.shift.pk, counted_cash=Decimal("0"))

    def test_closing_twice_is_refused(self):
        services.close_shift(actor=self.s.counter, request=None, shift_id=self.s.shift.pk, counted_cash=Decimal("500"))
        with self.assertRaises(services.SaleError):
            services.close_shift(actor=self.s.counter, request=None, shift_id=self.s.shift.pk, counted_cash=Decimal("500"))


class CartTests(Base):
    def test_adding_the_same_item_again_adds_to_the_quantity(self):
        self.s.cart(qty=2)
        draft = self.s.cart(qty=3)
        self.assertEqual(draft.lines.get().quantity, 5)

    def test_cannot_put_more_in_the_bill_than_can_be_sold(self):
        with self.assertRaises(services.SaleError) as ctx:
            self.s.cart(qty=101)
        self.assertIn("Only 100", str(ctx.exception))
        self.s.cart(qty=60)
        with self.assertRaises(services.SaleError):
            self.s.cart(qty=41)

    def test_expired_and_recalled_stock_is_not_counted(self):
        med = self.s.medicine("OldStock", "")
        self.s.batch(med, qty=10, days=-1, number="E1")
        self.s.batch(med, qty=10, days=100, number="R1", recalled=True)
        with self.assertRaises(services.SaleError) as ctx:
            self.s.cart(med, 1)
        self.assertIn("Only 0", str(ctx.exception))

    def test_items_the_counter_may_not_sell(self):
        x = self.s.medicine("Xdrug", "", schedule_category="X")
        self.s.batch(x, qty=5)
        with self.assertRaises(services.SaleError) as ctx:
            self.s.cart(x, 1)
        self.assertIn("Schedule X", str(ctx.exception))
        clinic_only = self.s.medicine("ClinicOnly", "", counter_sale_allowed=False)
        self.s.batch(clinic_only, qty=5)
        with self.assertRaises(services.SaleError):
            self.s.cart(clinic_only, 1)
        gone = self.s.medicine("Gone", "", is_active=False)
        self.s.batch(gone, qty=5)
        with self.assertRaises(services.SaleError):
            self.s.cart(gone, 1)

    def test_quantity_must_be_at_least_one(self):
        with self.assertRaises(services.SaleError):
            self.s.cart(qty=0)

    def test_change_and_remove_lines_and_discard(self):
        draft = self.s.cart(qty=2)
        line = draft.lines.get()
        services.set_quantity(actor=self.s.counter, request=None, line_id=line.pk, quantity=7)
        line.refresh_from_db()
        self.assertEqual(line.quantity, 7)
        with self.assertRaises(services.SaleError):
            services.set_quantity(actor=self.s.counter, request=None, line_id=line.pk, quantity=1000)
        services.remove_line(actor=self.s.counter, request=None, line_id=line.pk)
        self.assertEqual(draft.lines.count(), 0)
        self.s.cart(qty=1)
        services.discard_draft(actor=self.s.counter, request=None)
        self.assertIsNone(services.get_draft(self.s.counter))

    def test_nobody_else_can_change_my_bill(self):
        line = self.s.cart(qty=2).lines.get()
        other = make_user("c2", "counter")
        with self.assertRaises(PermissionDenied):
            services.set_quantity(actor=other, request=None, line_id=line.pk, quantity=1)

    def test_search_shows_location_stock_price_and_nothing_secret(self):
        row = search.search_items("parac")[0]
        self.assertEqual((row["location"], row["usable"], row["price"], row["batch_number"]),
                         ("Rack R02 \u2192 Shelf S03 \u2192 Box B12", 100, Decimal("2.0000"), "PB1"))
        self.assertFalse({k for k in row} & {"cost", "purchase_price", "unit_cost", "supplier", "margin"})
        self.assertEqual(len(search.search_items("R02")), 1)    # by rack
        self.assertEqual(len(search.search_items("s03")), 1)    # by shelf
        self.assertEqual(len(search.search_items("B12")), 1)    # by box
        self.assertEqual(len(search.search_items("500 mg parac")), 1)
        self.assertEqual(search.search_items("nothing-like-this"), [])
        for b in search.public_batches(self.s.med.pk):
            self.assertEqual(set(b), {"batch_number", "expiry_date", "quantity_on_hand", "selling_price_per_unit"})

    def test_search_flags_near_expiry_prescription_only_and_out_of_stock(self):
        near = self.s.medicine("NearOne", "")
        self.s.batch(near, qty=3, days=20)
        rx = self.s.medicine("RxOne", "", schedule_category="H1")
        self.s.batch(rx, qty=3, days=200)
        none = self.s.medicine("NoStock", "")
        by = {r["name"]: r for r in (search.search_items("NearOne") + search.search_items("RxOne")
                                      + search.search_items("NoStock"))}
        self.assertTrue(by["NearOne"]["near_expiry"])
        self.assertTrue(by["RxOne"]["needs_approval"])
        self.assertEqual(by["NoStock"]["usable"], 0)

    def test_items_not_for_the_counter_are_not_listed(self):
        self.s.medicine("ClinicOnly", "", counter_sale_allowed=False)
        self.assertEqual(search.search_items("ClinicOnly"), [])


class CompleteSaleTests(Base):
    def test_a_simple_counter_sale(self):
        sale = self.s.sell(5, customer_name="Walk-in customer")
        self.assertEqual((sale.status, sale.channel, sale.total, sale.amount_paid), ("COMPLETED", COUNTER, Decimal("10.00"), Decimal("10.00")))
        self.assertEqual(sale.number, f"CS-{self.s.today.year}-000001")
        self.assertIsNone(sale.patient_id)
        self.assertIsNone(sale.prescription_id)
        self.assertEqual((sale.cashier, sale.shift, sale.customer_name), (self.s.counter, self.s.shift, "Walk-in customer"))
        self.assertEqual(self.on_hand(self.s.stock_batch), 95)
        line = sale.lines.get()
        self.assertEqual((line.batch_number, line.quantity, line.unit_price, line.line_total), ("PB1", 5, Decimal("2.0000"), Decimal("10.00")))
        self.assertEqual(line.unit_cost, Decimal("1.5000"))  # kept for the manager's profit reports, never shown at the counter
        pay = sale.payments.get()
        self.assertEqual((pay.method, pay.amount, pay.kind, pay.channel, pay.shift, pay.received_by),
                         ("CASH", Decimal("10.00"), "PAYMENT", COUNTER, self.s.shift, self.s.counter))
        self.assertEqual(services.get_draft(self.s.counter), None)
        self.assertEqual(stock.reconcile(), [])

    def test_the_ledger_records_the_counter_channel_and_the_bill(self):
        sale = self.s.sell(5)
        row = InventoryTransaction.objects.get(kind="DISPENSE")
        self.assertEqual((row.channel, row.reference_type, row.reference_id, row.quantity_change, row.created_by),
                         (COUNTER, "sale", str(sale.pk), -5, self.s.counter))

    def test_clinic_and_counter_sales_take_from_the_same_stock(self):
        stock.consume_stock(actor=self.s.clinic_pharm, request=None, medicine_id=self.s.med.pk, quantity=10,
                            channel=SalesChannel.CLINIC_PHARMACY, reference_type="clinic-test")
        self.s.sell(5)
        self.assertEqual(self.on_hand(self.s.stock_batch), 85)  # 100 - 10 (clinic) - 5 (counter)
        rows = InventoryTransaction.objects.filter(kind="DISPENSE")
        self.assertEqual({r.channel: r.quantity_change for r in rows},
                         {"CLINIC_PHARMACY": -10, "INDEPENDENT_COUNTER": -5})
        self.assertEqual(stock.reconcile(), [])
        self.assertEqual(Sale.objects.filter(channel="CLINIC_PHARMACY").count(), 0)  # a counter sale is never a clinic sale

    def test_earliest_expiry_goes_first_and_each_batch_keeps_its_own_price(self):
        med = self.s.medicine("TwoPrices", "")
        a = self.s.batch(med, qty=3, days=10, number="A", sell="2.5")
        b = self.s.batch(med, qty=10, days=100, number="B", sell="3")
        sale = self.s.sell(5, med=med)
        self.assertEqual([(l.batch_number, l.quantity, l.line_total) for l in sale.lines.order_by("id")],
                         [("A", 3, Decimal("7.50")), ("B", 2, Decimal("6.00"))])
        self.assertEqual(sale.total, Decimal("13.50"))
        self.assertEqual((self.on_hand(a), self.on_hand(b)), (0, 8))

    def test_expired_stock_cannot_be_sold(self):
        med = self.s.medicine("Expired", "")
        self.s.batch(med, qty=10, days=-1)
        with self.assertRaises(services.SaleError):
            self.s.sell(1, med=med)

    def test_stock_that_disappears_before_checkout_stops_the_sale_and_changes_nothing(self):
        draft = self.s.cart(qty=5)
        stock.consume_stock(actor=self.s.clinic_pharm, request=None, medicine_id=self.s.med.pk, quantity=98,
                            channel=SalesChannel.CLINIC_PHARMACY)
        with self.assertRaises(services.SaleError) as ctx:
            services.complete_counter_sale(actor=self.s.counter, request=None, sale_id=draft.pk,
                                           payments=self.s.pay(Decimal("10.00")))
        self.assertIn("Only 2", str(ctx.exception))
        self.assertEqual(self.on_hand(self.s.stock_batch), 2)
        draft.refresh_from_db()
        self.assertEqual(draft.status, "DRAFT")
        self.assertEqual(Payment.objects.count(), 0)

    def test_a_retry_or_double_click_never_takes_stock_or_money_twice(self):
        draft = self.s.cart(qty=5)
        first = services.complete_counter_sale(actor=self.s.counter, request=None, sale_id=draft.pk, payments=self.s.pay(Decimal("10.00")))
        again = services.complete_counter_sale(actor=self.s.counter, request=None, sale_id=draft.pk, payments=self.s.pay(Decimal("10.00")))
        self.assertEqual(first.pk, again.pk)
        self.assertEqual(self.on_hand(self.s.stock_batch), 95)
        self.assertEqual((Payment.objects.count(), InventoryTransaction.objects.filter(kind="DISPENSE").count()), (1, 1))
        self.assertEqual(Sale.objects.filter(status="COMPLETED").count(), 1)

    def test_an_unknown_bill_number_is_a_message_not_a_crash(self):
        with self.assertRaises(services.SaleError):
            services.complete_counter_sale(actor=self.s.counter, request=None, sale_id=987654, payments=self.s.pay(Decimal("1")))
        with self.assertRaises(services.SaleError):
            services.set_quantity(actor=self.s.counter, request=None, line_id=987654, quantity=1)

    def test_bill_numbers_follow_on(self):
        self.assertEqual([self.s.sell(1).number[-6:], self.s.sell(1).number[-6:]], ["000001", "000002"])

    def test_nobody_else_can_complete_my_bill(self):
        draft = self.s.cart(qty=1)
        other = make_user("c2", "counter")
        self.s.open_shift_for(other)
        with self.assertRaises(services.SaleError) as ctx:
            services.complete_counter_sale(actor=other, request=None, sale_id=draft.pk, payments=self.s.pay(Decimal("2.00")))
        self.assertIn("another cashier", str(ctx.exception))

    def test_only_sellers_can_complete(self):
        draft = self.s.cart(qty=1)
        for user in (self.s.doctor, self.s.reception, self.s.clinic_pharm, self.s.admin):
            with self.assertRaises(PermissionDenied):
                services.complete_counter_sale(actor=user, request=None, sale_id=draft.pk, payments=self.s.pay(Decimal("2.00")))

    def test_empty_bill_and_audit_trail(self):
        draft = services._get_or_create_draft(self.s.counter)
        with self.assertRaises(services.SaleError):
            services.complete_counter_sale(actor=self.s.counter, request=None, sale_id=draft.pk, payments=self.s.pay(Decimal("1")))
        sale = self.s.sell(2)
        ev = AuditEvent.objects.get(action="sale.complete")
        self.assertEqual((ev.username, ev.object_id), ("counter1", str(sale.pk)))


class PaymentTests(Base):
    def attempt(self, payments, qty=5):
        draft = self.s.cart(qty=qty)
        return services.complete_counter_sale(actor=self.s.counter, request=None, sale_id=draft.pk, payments=payments)

    def assertNothingChanged(self):
        self.assertEqual(self.on_hand(self.s.stock_batch), 100)
        self.assertEqual((Payment.objects.count(), Sale.objects.filter(status="COMPLETED").count(),
                          InventoryTransaction.objects.filter(kind="DISPENSE").count()), (0, 0, 0))

    def test_a_split_payment_must_add_up_to_the_total(self):
        sale = self.attempt([{"method": "CASH", "amount": Decimal("4.00")},
                             {"method": "UPI", "amount": Decimal("6.00"), "confirmed": True, "reference": "UPI123"}])
        self.assertEqual(sorted((p.method, p.amount, p.reference) for p in sale.payments.all()),
                         [("CASH", Decimal("4.00"), ""), ("UPI", Decimal("6.00"), "UPI123")])

    def test_short_or_excess_payment_is_refused_and_nothing_is_taken(self):
        for amount in ("9.99", "10.01"):
            with self.assertRaises(services.SaleError) as ctx:
                self.attempt([{"method": "CASH", "amount": Decimal(amount)}])
            self.assertIn("must add up", str(ctx.exception))
            services.discard_draft(actor=self.s.counter, request=None)
        self.assertNothingChanged()

    def test_upi_and_card_need_the_received_tick(self):
        for method in ("UPI", "CARD"):
            with self.assertRaises(services.SaleError):
                self.attempt([{"method": method, "amount": Decimal("10.00"), "confirmed": False}])
            services.discard_draft(actor=self.s.counter, request=None)
        self.assertNothingChanged()

    def test_a_method_the_clinic_has_switched_off_is_refused(self):
        Setting.objects.create(key="enabled_payment_methods", value=["CASH"])
        with self.assertRaises(services.SaleError):
            self.attempt([{"method": "CARD", "amount": Decimal("10.00"), "confirmed": True}])
        self.assertNothingChanged()

    def test_no_payment_or_zero_payment_is_refused(self):
        for payments in ([], [{"method": "CASH", "amount": Decimal("0")}]):
            with self.assertRaises(services.SaleError):
                self.attempt(payments)
            services.discard_draft(actor=self.s.counter, request=None)
        self.assertNothingChanged()

    def test_the_price_check_stops_a_bill_that_changed_while_open(self):
        draft = self.s.cart(qty=5)
        with self.assertRaises(services.SaleError) as ctx:
            services.complete_counter_sale(actor=self.s.counter, request=None, sale_id=draft.pk, payments=self.s.pay(Decimal("10.00")),
                                           expected_subtotal=Decimal("99.00"))
        self.assertIn("changed", str(ctx.exception))
        self.assertNothingChanged()


class DiscountTests(Base):
    def test_a_discount_within_the_limit_with_a_reason(self):
        sale = self.s.sell(10, discount_percent=5, discount_reason="Regular customer")
        self.assertEqual((sale.subtotal, sale.discount_percent, sale.discount_total, sale.total),
                         (Decimal("20.00"), Decimal("5.00"), Decimal("1.00"), Decimal("19.00")))
        self.assertEqual((sale.discount_reason, sale.lines.get().discount_amount), ("Regular customer", Decimal("1.00")))

    def test_a_reason_is_required_and_the_limit_is_enforced_per_role(self):
        with self.assertRaises(services.SaleError):
            self.s.sell(10, discount_percent=5, discount_reason="  ")
        services.discard_draft(actor=self.s.counter, request=None)
        with self.assertRaises(services.SaleError) as ctx:
            self.s.sell(10, discount_percent=6, discount_reason="Please")
        self.assertIn("above 5", str(ctx.exception))
        services.discard_draft(actor=self.s.counter, request=None)
        self.assertEqual(Sale.objects.filter(status="COMPLETED").count(), 0)
        self.assertEqual(self.on_hand(self.s.stock_batch), 100)
        self.s.open_shift_for(self.s.pharm)
        sale = self.s.sell(10, user=self.s.pharm, discount_percent=8, discount_reason="Senior citizen")  # manager's limit is 10
        self.assertEqual(sale.total, Decimal("18.40"))
        services.discard_draft(actor=self.s.pharm, request=None)
        with self.assertRaises(services.SaleError):
            self.s.sell(10, user=self.s.pharm, discount_percent=11, discount_reason="Too much")

    def test_the_limits_can_be_changed_in_settings(self):
        Setting.objects.create(key="counter_discount_limit_percent", value=8)
        self.assertEqual(self.s.sell(10, discount_percent=7, discount_reason="Offer").discount_total, Decimal("1.40"))

    def test_discount_is_rounded_per_line_and_totals_add_up(self):
        med = self.s.medicine("Odd", "")
        self.s.batch(med, qty=10, days=100, sell="3.3333")
        sale = self.s.sell(3, med=med, discount_percent=5, discount_reason="Offer")
        self.assertEqual((sale.subtotal, sale.discount_total, sale.total), (Decimal("10.00"), Decimal("0.50"), Decimal("9.50")))

    def test_only_roles_with_discount_permission_can_use_it(self):
        draft = self.s.cart(qty=1)
        with self.assertRaises(PermissionDenied):
            services._validate_discount(self.s.doctor, 5, "x")
        self.assertEqual(services._validate_discount(self.s.counter, 0, ""), Decimal("0.00"))
        draft.delete()


class PrescriptionOnlyTests(Base):
    def setUp(self):
        super().setUp()
        self.rx = self.s.medicine("Amoxycillin", "500 mg", schedule_category="H")
        self.rx_batch = self.s.batch(self.rx, qty=50, days=200, number="RX1", sell="4")

    def attempt(self, restricted, med=None, qty=2):
        draft = self.s.cart(med or self.rx, qty)
        return services.complete_counter_sale(actor=self.s.counter, request=None, sale_id=draft.pk,
                                              payments=self.s.pay(self.s.total_of(draft)), restricted=restricted)

    def test_without_the_prescription_details_and_approval_it_cannot_be_sold(self):
        with self.assertRaises(services.SaleError) as ctx:
            self.attempt(None)
        self.assertIn("prescription-only", str(ctx.exception))
        with self.assertRaises(services.SaleError):
            self.attempt(self.s.restricted_data(prescriber_reg_no=""))
        self.assertEqual(self.on_hand(self.rx_batch), 50)

    def test_wrong_password_or_unauthorised_approver_is_refused(self):
        for data, text in ((self.s.restricted_data(approver_password="wrong-password-1"), "approval failed"),
                           (self.s.restricted_data(approver_username="rita"), "not allowed to approve"),       # reception
                           (self.s.restricted_data(approver_username="counter1"), "not allowed to approve"),   # the seller herself
                           (self.s.restricted_data(approver_username="doc"), "not allowed to approve")):
            with self.assertRaises(services.SaleError) as ctx:
                self.attempt(data)
            self.assertIn(text, str(ctx.exception))
        self.assertEqual(self.on_hand(self.rx_batch), 50)
        self.assertEqual(Sale.objects.filter(status="COMPLETED").count(), 0)

    def test_an_inactive_pharmacist_cannot_approve(self):
        self.s.clinic_pharm.is_active = False
        self.s.clinic_pharm.save()
        with self.assertRaises(services.SaleError):
            self.attempt(self.s.restricted_data())

    def test_the_clinic_pharmacist_or_the_manager_can_approve_and_it_is_recorded(self):
        sale = self.attempt(self.s.restricted_data())
        self.assertEqual((sale.rx_prescriber_name, sale.rx_prescriber_reg_no, sale.rx_patient_name),
                         ("Dr Test Prescriber", "TEST-REG-77", "Walk In Patient"))
        self.assertEqual((sale.rx_approved_by, sale.total), (self.s.clinic_pharm, Decimal("8.00")))
        self.assertIsNotNone(sale.rx_approved_at)
        self.assertIsNone(sale.patient_id)  # the patient's NAME is on the bill, but the sale is still not a clinic patient
        ev = AuditEvent.objects.get(action="sale.restricted_approved")
        self.assertIn("cpharm", ev.summary)
        self.assertFalse(any("Walk In Patient" in f"{e.summary} {e.changes}" for e in AuditEvent.objects.all()))
        manager_sale = self.attempt(self.s.restricted_data(approver_username="pharm"))
        self.assertEqual(manager_sale.rx_approved_by, self.s.pharm)

    def test_one_prescription_only_item_makes_the_whole_bill_need_approval(self):
        self.s.cart(self.s.med, 1)
        draft = self.s.cart(self.rx, 1)
        with self.assertRaises(services.SaleError):
            services.complete_counter_sale(actor=self.s.counter, request=None, sale_id=draft.pk,
                                           payments=self.s.pay(self.s.total_of(draft)))

    def test_ordinary_medicines_need_no_approval(self):
        self.assertIsNone(self.s.sell(1).rx_approved_by_id)

    def test_schedule_x_is_blocked_unless_the_clinic_allows_it_and_then_still_needs_approval(self):
        x = self.s.medicine("Xdrug", "", schedule_category="X")
        self.s.batch(x, qty=5, sell="10")
        with self.assertRaises(services.SaleError):
            self.s.cart(x, 1)
        Setting.objects.create(key="counter_allow_schedule_x", value=True)
        draft = self.s.cart(x, 1)
        with self.assertRaises(services.SaleError):
            services.complete_counter_sale(actor=self.s.counter, request=None, sale_id=draft.pk, payments=self.s.pay(Decimal("10.00")))
        done = services.complete_counter_sale(actor=self.s.counter, request=None, sale_id=draft.pk, payments=self.s.pay(Decimal("10.00")),
                                              restricted=self.s.restricted_data())
        self.assertEqual(done.status, "COMPLETED")


class RecordsAreProtectedTests(Base):
    def test_a_counter_sale_can_never_be_linked_to_a_patient(self):
        patient = make_patient(self.s.reception)
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                Sale.objects.create(channel=COUNTER, cashier=self.s.pharm, patient=patient)

    def test_one_open_bill_per_cashier(self):
        services._get_or_create_draft(self.s.counter)
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                Sale.objects.create(channel=COUNTER, cashier=self.s.counter)

    def test_a_completed_sale_cannot_be_edited_or_deleted(self):
        sale = self.s.sell(2)
        line = sale.lines.get()
        line.quantity = 99
        with self.assertRaises(SaleLockedError):
            line.save()
        with self.assertRaises(SaleLockedError):
            line.delete()
        sale.discount_reason = "tampered"
        with self.assertRaises(SaleLockedError):
            sale.save()
        with self.assertRaises(SaleLockedError):
            Sale.objects.get(pk=sale.pk).delete()
        with self.assertRaises(SaleLockedError):
            SaleLine(sale=sale, medicine=self.s.med, quantity=1).save()

    def test_payments_cannot_be_edited_or_deleted(self):
        sale = self.s.sell(2)
        pay = sale.payments.get()
        pay.amount = Decimal("1")
        with self.assertRaises(ImmutableRecordError):
            pay.save()
        with self.assertRaises(ImmutableRecordError):
            pay.delete()
        with self.assertRaises(ImmutableRecordError):
            Payment.objects.all().update(amount=1)
        with self.assertRaises(ImmutableRecordError):
            Payment.objects.all().delete()

    @unittest.skipUnless(connection.vendor == "postgresql", "Triggers exist only on PostgreSQL")
    def test_the_database_also_blocks_changes_to_payments(self):
        self.s.sell(2)
        for sql in ("UPDATE sales_payment SET amount = 1", "DELETE FROM sales_payment"):
            with self.assertRaises(DatabaseError):
                with transaction.atomic():
                    with connection.cursor() as cur:
                        cur.execute(sql)
        self.assertEqual(Payment.objects.count(), 1)
