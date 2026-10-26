import threading
import unittest
from datetime import timedelta
from decimal import Decimal
from io import StringIO

from django.core.exceptions import PermissionDenied
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import DatabaseError, IntegrityError, connection, connections, transaction
from django.test import TestCase, TransactionTestCase

from apps.core.models import AuditEvent, ImmutableRecordError, Setting
from apps.pharmacy import stock
from apps.pharmacy.models import InventoryTransaction, MedicineBatch

from .helpers import Shop


def consume(shop, med, qty, **kw):
    return stock.consume_stock(actor=shop.pharm, request=None, medicine_id=med.pk, quantity=qty, **kw)


class FefoTests(TestCase):
    def setUp(self):
        self.s = Shop()
        self.med = self.s.medicine()

    def on_hand(self, b):
        b.refresh_from_db()
        return b.quantity_on_hand

    def test_earliest_expiry_goes_first_across_batches(self):
        late = self.s.batch(self.med, qty=5, days=60, number="LATE")
        early = self.s.batch(self.med, qty=5, days=10, number="EARLY")
        mid = self.s.batch(self.med, qty=5, days=30, number="MID")
        result = consume(self.s, self.med, 7)
        self.assertEqual([(a.batch.batch_number, a.quantity) for a in result.allocations], [("EARLY", 5), ("MID", 2)])
        self.assertEqual((self.on_hand(early), self.on_hand(mid), self.on_hand(late)), (0, 3, 5))
        self.assertEqual(result.shortfall, 0)

    def test_same_expiry_uses_the_batch_received_first(self):
        old = self.s.batch(self.med, qty=5, days=30, number="OLD", received=self.s.today - timedelta(days=10))
        new = self.s.batch(self.med, qty=5, days=30, number="NEW")
        result = consume(self.s, self.med, 3)
        self.assertEqual(result.allocations[0].batch, old)
        self.assertEqual(self.on_hand(new), 5)

    def test_ledger_rows_record_every_take(self):
        self.s.batch(self.med, qty=5, days=10, number="A")
        self.s.batch(self.med, qty=5, days=20, number="B")
        consume(self.s, self.med, 7, reference_type="dispensing", reference_id=42)
        rows = list(InventoryTransaction.objects.filter(kind="DISPENSE").order_by("id"))
        self.assertEqual([(r.batch.batch_number, r.quantity_change, r.balance_after) for r in rows], [("A", -5, 0), ("B", -2, 3)])
        self.assertEqual((rows[0].reference_type, rows[0].reference_id, rows[0].created_by), ("dispensing", "42", self.s.pharm))
        self.assertEqual(stock.reconcile(), [])

    def test_expired_stock_is_never_dispensed_and_failure_changes_nothing(self):
        expired = self.s.batch(self.med, qty=10, days=-1, number="OLD")
        good = self.s.batch(self.med, qty=5, days=100, number="GOOD")
        with self.assertRaises(stock.StockError) as ctx:
            consume(self.s, self.med, 8)
        self.assertIn("Only 5", str(ctx.exception))
        self.assertEqual((self.on_hand(expired), self.on_hand(good)), (10, 5))
        self.assertEqual(InventoryTransaction.objects.filter(kind="DISPENSE").count(), 0)

    def test_expiry_date_itself_is_still_usable_but_yesterday_is_not(self):
        today_batch = self.s.batch(self.med, qty=3, days=0, number="TODAY")
        self.s.batch(self.med, qty=3, days=-1, number="YESTERDAY")
        result = consume(self.s, self.med, 3)
        self.assertEqual(result.allocations[0].batch, today_batch)
        with self.assertRaises(stock.StockError):
            consume(self.s, self.med, 1)

    def test_recalled_batches_are_skipped_until_cleared(self):
        bad = self.s.batch(self.med, qty=5, days=5, number="BAD", recalled=True)
        self.s.batch(self.med, qty=5, days=50, number="OK")
        self.assertEqual(consume(self.s, self.med, 2).allocations[0].batch.batch_number, "OK")
        stock.set_recall(actor=self.s.pharm, request=None, batch_id=bad.pk, recalled=False)
        self.assertEqual(consume(self.s, self.med, 1).allocations[0].batch.batch_number, "BAD")

    def test_minimum_days_before_expiry_setting_blocks_short_dated_stock(self):
        self.s.batch(self.med, qty=5, days=20, number="SHORT")
        self.assertEqual(consume(self.s, self.med, 1).allocations[0].batch.batch_number, "SHORT")
        Setting.objects.create(key="dispense_min_days_before_expiry", value=30)
        with self.assertRaises(stock.StockError):
            consume(self.s, self.med, 1)

    def test_near_expiry_is_flagged_but_still_dispensable(self):
        b = self.s.batch(self.med, qty=5, days=45, number="NEAR")
        self.assertEqual(stock.batch_status(b), "NEAR_EXPIRY")
        self.assertEqual(stock.batch_status(self.s.batch(self.med, days=200, number="FAR")), "OK")
        self.assertEqual(stock.batch_status(self.s.batch(self.med, days=-3, number="EXP")), "EXPIRED")
        self.assertEqual(consume(self.s, self.med, 2).shortfall, 0)

    def test_partial_fill_returns_what_exists(self):
        self.s.batch(self.med, qty=4, days=30)
        result = consume(self.s, self.med, 10, allow_partial=True)
        self.assertEqual((sum(a.quantity for a in result.allocations), result.shortfall), (4, 6))
        none = consume(self.s, self.med, 5, allow_partial=True)
        self.assertEqual((none.allocations, none.shortfall), ([], 5))

    def test_quantity_must_be_positive(self):
        self.s.batch(self.med, qty=4)
        for bad in (0, -2):
            with self.assertRaises(stock.StockError):
                consume(self.s, self.med, bad)

    def test_only_pharmacy_dispenses(self):
        self.s.batch(self.med, qty=4)
        for user in (self.s.doctor, self.s.reception, self.s.admin):
            with self.assertRaises(PermissionDenied):
                stock.consume_stock(actor=user, request=None, medicine_id=self.med.pk, quantity=1)

    def test_preview_changes_nothing(self):
        b = self.s.batch(self.med, qty=4)
        plan, short = stock.preview_fefo(self.med.pk, 3)
        self.assertEqual(([(x.pk, n) for x, n in plan], short), ([(b.pk, 3)], 0))
        self.assertEqual(self.on_hand(b), 4)

    def test_allocation_carries_prices_for_billing(self):
        self.s.batch(self.med, qty=4, cost="2.5", sell="3.75")
        a = consume(self.s, self.med, 1).allocations[0]
        self.assertEqual((a.unit_price, a.unit_cost), (Decimal("3.75"), Decimal("2.5")))


class ManualChangeTests(TestCase):
    def setUp(self):
        self.s = Shop()
        self.med = self.s.medicine()
        self.batch = self.s.batch(self.med, qty=10, days=100)

    def test_adjust_wastage_and_return_move_stock_and_write_the_ledger(self):
        pk = self.batch.pk
        stock.adjust_stock(actor=self.s.pharm, request=None, batch_id=pk, delta=-2, reason="Stock-take")
        stock.record_wastage(actor=self.s.pharm, request=None, batch_id=pk, quantity=3, reason="Damaged")
        stock.return_to_supplier(actor=self.s.admin, request=None, batch_id=pk, quantity=1, reason="Wrong item")
        stock.adjust_stock(actor=self.s.admin, request=None, batch_id=pk, delta=4, reason="Found 4 more")
        self.batch.refresh_from_db()
        self.assertEqual(self.batch.quantity_on_hand, 10 - 2 - 3 - 1 + 4)
        kinds = list(InventoryTransaction.objects.exclude(kind="RECEIPT").order_by("id").values_list("kind", "quantity_change", "balance_after"))
        self.assertEqual(kinds, [("ADJUSTMENT", -2, 8), ("WASTAGE", -3, 5), ("SUPPLIER_RETURN", -1, 4), ("ADJUSTMENT", 4, 8)])
        self.assertEqual(stock.reconcile(), [])

    def test_cannot_remove_more_than_is_there_or_use_zero_or_skip_reason(self):
        pk = self.batch.pk
        with self.assertRaises(stock.StockError):
            stock.record_wastage(actor=self.s.pharm, request=None, batch_id=pk, quantity=11, reason="x")
        with self.assertRaises(stock.StockError):
            stock.adjust_stock(actor=self.s.pharm, request=None, batch_id=pk, delta=0, reason="x")
        with self.assertRaises(stock.StockError):
            stock.adjust_stock(actor=self.s.pharm, request=None, batch_id=pk, delta=-1, reason="  ")
        self.batch.refresh_from_db()
        self.assertEqual(self.batch.quantity_on_hand, 10)

    def test_expired_stock_can_be_written_off(self):
        old = self.s.batch(self.med, qty=6, days=-5, number="OLD")
        stock.record_wastage(actor=self.s.pharm, request=None, batch_id=old.pk, quantity=6, reason="Expired")
        old.refresh_from_db()
        self.assertEqual(old.quantity_on_hand, 0)

    def test_permissions(self):
        pk = self.batch.pk
        for user in (self.s.doctor, self.s.reception):
            with self.assertRaises(PermissionDenied):
                stock.adjust_stock(actor=user, request=None, batch_id=pk, delta=-1, reason="x")
            with self.assertRaises(PermissionDenied):
                stock.return_to_supplier(actor=user, request=None, batch_id=pk, quantity=1, reason="x")
            with self.assertRaises(PermissionDenied):
                stock.set_recall(actor=user, request=None, batch_id=pk, recalled=True, reason="x")
        self.batch.refresh_from_db()
        self.assertEqual(self.batch.quantity_on_hand, 10)

    def test_manual_changes_are_audited_without_free_text(self):
        stock.adjust_stock(actor=self.s.pharm, request=None, batch_id=self.batch.pk, delta=-1, reason="Private reason text")
        ev = AuditEvent.objects.get(action="stock.adjust")
        self.assertNotIn("Private", f"{ev.summary} {ev.changes} {ev.object_repr}")

    def test_recall_needs_reason_and_is_recorded(self):
        with self.assertRaises(stock.StockError):
            stock.set_recall(actor=self.s.pharm, request=None, batch_id=self.batch.pk, recalled=True, reason="")
        b = stock.set_recall(actor=self.s.pharm, request=None, batch_id=self.batch.pk, recalled=True, reason="Manufacturer notice")
        self.assertEqual((b.is_recalled, b.recalled_by), (True, self.s.pharm))
        self.assertEqual(stock.batch_status(b), "RECALLED")


class IntegrityTests(TestCase):
    def setUp(self):
        self.s = Shop()
        self.med = self.s.medicine()
        self.batch = self.s.batch(self.med, qty=5)

    def test_database_refuses_negative_stock(self):
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                MedicineBatch.objects.filter(pk=self.batch.pk).update(quantity_on_hand=-1)

    def test_database_refuses_wrong_sign_ledger_rows(self):
        for kind, change in (("DISPENSE", 3), ("RECEIPT", -3), ("WASTAGE", 1), ("SUPPLIER_RETURN", 2), ("ADJUSTMENT", 0)):
            with self.assertRaises(IntegrityError):
                with transaction.atomic():
                    InventoryTransaction.objects.create(batch=self.batch, medicine=self.med, kind=kind, quantity_change=change,
                                                        balance_after=1, unit_cost=Decimal("1"))

    def test_ledger_cannot_be_edited_or_deleted(self):
        row = InventoryTransaction.objects.first()
        row.reason = "tampered"
        with self.assertRaises(ImmutableRecordError):
            row.save()
        with self.assertRaises(ImmutableRecordError):
            row.delete()
        with self.assertRaises(ImmutableRecordError):
            InventoryTransaction.objects.all().update(reason="x")

    @unittest.skipUnless(connection.vendor == "postgresql", "Triggers exist only on PostgreSQL")
    def test_database_blocks_raw_sql_on_the_ledger(self):
        for sql in ("UPDATE pharmacy_inventorytransaction SET reason = 'x'", "DELETE FROM pharmacy_inventorytransaction"):
            with self.assertRaises(DatabaseError):
                with transaction.atomic():
                    with connection.cursor() as cur:
                        cur.execute(sql)
        self.assertEqual(InventoryTransaction.objects.count(), 1)

    def test_reconcile_and_check_command_detect_tampering(self):
        out = StringIO()
        call_command("check_stock", stdout=out)
        self.assertIn("agree", out.getvalue())
        MedicineBatch.objects.filter(pk=self.batch.pk).update(quantity_on_hand=99)
        self.assertEqual(stock.reconcile()[0]["id"], self.batch.pk)
        with self.assertRaises(CommandError):
            call_command("check_stock", stdout=StringIO(), stderr=StringIO())


class OverviewAndValuationTests(TestCase):
    def setUp(self):
        self.s = Shop()

    def row(self, med, **kw):
        return next(r for r in stock.medicine_stock_rows(**kw) if r["medicine"] == med)

    def test_flags(self):
        low = self.s.medicine("Low", reorder_threshold=20)
        self.s.batch(low, qty=15, days=200)
        out = self.s.medicine("Out", reorder_threshold=10)
        near = self.s.medicine("Near")
        self.s.batch(near, qty=8, days=30)
        exp = self.s.medicine("Exp")
        self.s.batch(exp, qty=4, days=-2)
        rec = self.s.medicine("Rec")
        self.s.batch(rec, qty=6, days=100, recalled=True)
        r = self.row(low)
        self.assertEqual((r["usable"], r["low"], r["out"]), (15, True, False))
        self.assertTrue(self.row(out)["out"])
        n = self.row(near)
        self.assertEqual((n["usable"], n["near"]), (8, 8))
        e = self.row(exp)
        self.assertEqual((e["usable"], e["expired"], e["out"]), (0, 4, True))
        c = self.row(rec)
        self.assertEqual((c["usable"], c["recalled"]), (0, 6))
        self.assertEqual({r["medicine"].brand_name for r in stock.medicine_stock_rows(show="low")}, {"Low", "Out"})
        self.assertEqual({r["medicine"].brand_name for r in stock.medicine_stock_rows(show="near")}, {"Near"})
        self.assertEqual({r["medicine"].brand_name for r in stock.medicine_stock_rows(show="expired")}, {"Exp"})
        self.assertIn("Out", {r["medicine"].brand_name for r in stock.medicine_stock_rows(show="out")})

    def test_valuation_splits_usable_expired_and_recalled(self):
        m = self.s.medicine()
        self.s.batch(m, qty=10, days=100, number="U", cost="2.5", sell="3")
        self.s.batch(m, qty=4, days=-1, number="E", cost="2", sell="3")
        self.s.batch(m, qty=2, days=100, number="R", cost="10", sell="12", recalled=True)
        v = stock.valuation()
        self.assertEqual((v["cost_usable"], v["sell_usable"]), (Decimal("25.00"), Decimal("30.00")))
        self.assertEqual((v["cost_expired"], v["sell_expired"]), (Decimal("8.00"), Decimal("12.00")))
        self.assertEqual((v["cost_recalled"], v["sell_recalled"]), (Decimal("20.00"), Decimal("24.00")))
        self.assertEqual((v["cost_all"], v["sell_all"]), (Decimal("53.00"), Decimal("66.00")))
        by = stock.valuation_by_medicine()
        self.assertEqual((by[0]["units"], by[0]["cost"]), (16, Decimal("53.00")))

    def test_empty_stock_valuation_is_zero(self):
        self.assertEqual(stock.valuation()["cost_all"], Decimal("0.00"))


@unittest.skipUnless(connection.vendor == "postgresql", "Concurrency tests need PostgreSQL")
class ConcurrentDispensingTests(TransactionTestCase):
    def setUp(self):
        self.s = Shop()
        self.med = self.s.medicine()

    def run_threads(self, n, fn):
        results, errors = [], []
        barrier = threading.Barrier(n)

        def worker():
            try:
                barrier.wait()
                results.append(fn())
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)
            finally:
                connections.close_all()

        threads = [threading.Thread(target=worker) for _ in range(n)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        return results, errors

    def test_ten_simultaneous_sales_of_five_units_never_oversell(self):
        batch = self.s.batch(self.med, qty=5)
        results, errors = self.run_threads(10, lambda: consume(self.s, self.med, 1))
        self.assertEqual(len(results), 5)
        self.assertEqual(len(errors), 5)
        self.assertTrue(all(isinstance(e, stock.StockError) for e in errors), errors)
        batch.refresh_from_db()
        self.assertEqual(batch.quantity_on_hand, 0)
        self.assertEqual(InventoryTransaction.objects.filter(kind="DISPENSE").count(), 5)
        self.assertEqual(stock.reconcile(), [])

    def test_many_sales_across_several_batches_conserve_stock_without_deadlock(self):
        for i, days in enumerate((10, 20, 30)):
            self.s.batch(self.med, qty=4, days=days, number=f"B{i}")
        results, errors = self.run_threads(20, lambda: consume(self.s, self.med, 1))
        self.assertTrue(all(isinstance(e, stock.StockError) for e in errors), errors)
        self.assertEqual((len(results), len(errors)), (12, 8))
        self.assertEqual(sum(MedicineBatch.objects.values_list("quantity_on_hand", flat=True)), 0)
        self.assertEqual(stock.reconcile(), [])

    def test_multi_unit_sales_across_batches_stay_consistent(self):
        self.s.batch(self.med, qty=3, days=10, number="A")
        self.s.batch(self.med, qty=3, days=20, number="B")
        results, errors = self.run_threads(6, lambda: consume(self.s, self.med, 2))
        self.assertTrue(all(isinstance(e, stock.StockError) for e in errors), errors)
        self.assertEqual((len(results), len(errors)), (3, 3))
        self.assertEqual(sum(MedicineBatch.objects.values_list("quantity_on_hand", flat=True)), 0)
        self.assertEqual(stock.reconcile(), [])
