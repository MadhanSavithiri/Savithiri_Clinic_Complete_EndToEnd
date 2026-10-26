import threading
import unittest
from decimal import Decimal

from django.db import connection, connections
from django.test import TransactionTestCase

from apps.accounts.tests.helpers import make_user
from apps.core.models import SalesChannel
from apps.pharmacy import stock
from apps.pharmacy.models import InventoryTransaction, MedicineBatch
from apps.sales import services
from apps.sales.models import Payment, Sale

from .helpers import CounterShop


@unittest.skipUnless(connection.vendor == "postgresql", "Concurrency tests need PostgreSQL")
class ConcurrentCounterSalesTests(TransactionTestCase):
    def setUp(self):
        self.s = CounterShop()
        self.med = self.s.medicine("Scarce", "")

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

    def cashier_with_bill(self, i, qty):
        user = make_user(f"cc{i}", "counter")
        self.s.open_shift_for(user)
        return user, self.s.cart(self.med, qty, user).pk

    def finish(self, user, sale_id, amount):
        return lambda: services.complete_counter_sale(
            actor=user, request=None, sale_id=sale_id, payments=[{"method": "CASH", "amount": Decimal(amount)}])

    def test_ten_cashiers_racing_for_five_units_sell_exactly_five(self):
        batch = self.s.batch(self.med, qty=5, number="SC1", sell="2")
        targets = [self.finish(*self.cashier_with_bill(i, 1), "2.00") for i in range(10)]
        # (the arguments above are evaluated before the threads start)
        results, errors = self.run_threads(targets)
        self.assertEqual((len(results), len(errors)), (5, 5))
        self.assertTrue(all(isinstance(e, services.SaleError) for e in errors), errors)
        batch.refresh_from_db()
        self.assertEqual(batch.quantity_on_hand, 0)
        self.assertEqual((Sale.objects.filter(status="COMPLETED").count(), Payment.objects.count()), (5, 5))
        self.assertEqual(stock.reconcile(), [])

    def test_counter_and_clinic_selling_the_same_stock_never_oversell(self):
        batch = self.s.batch(self.med, qty=6, number="SC2", sell="2")
        counter_targets = [self.finish(*self.cashier_with_bill(i, 2), "4.00") for i in range(4)]
        clinic = lambda: stock.consume_stock(actor=self.s.clinic_pharm, request=None, medicine_id=self.med.pk, quantity=2,  # noqa: E731
                                             channel=SalesChannel.CLINIC_PHARMACY, reference_type="clinic-test")
        results, errors = self.run_threads(counter_targets + [clinic, clinic])
        self.assertTrue(all(isinstance(e, (services.SaleError, stock.StockError)) for e in errors), errors)
        self.assertEqual(len(results), 3)  # 6 units / 2 each
        batch.refresh_from_db()
        self.assertEqual(batch.quantity_on_hand, 0)
        sold = -sum(InventoryTransaction.objects.filter(kind="DISPENSE").values_list("quantity_change", flat=True))
        self.assertEqual(sold, 6)
        self.assertEqual(stock.reconcile(), [])

    def test_a_double_click_from_one_screen_sells_once(self):
        batch = self.s.batch(self.med, qty=5, number="SC3", sell="2")
        user, sale_id = self.cashier_with_bill(99, 2)
        results, errors = self.run_threads([self.finish(user, sale_id, "4.00")] * 3)
        self.assertEqual(errors, [])
        self.assertEqual({r.pk for r in results}, {sale_id})
        batch.refresh_from_db()
        self.assertEqual(batch.quantity_on_hand, 3)
        self.assertEqual((Payment.objects.count(), InventoryTransaction.objects.filter(kind="DISPENSE").count()), (1, 1))
