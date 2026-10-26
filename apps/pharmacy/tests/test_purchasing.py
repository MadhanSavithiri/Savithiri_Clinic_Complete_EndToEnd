import threading
import unittest
from datetime import timedelta
from decimal import Decimal

from django.core.exceptions import PermissionDenied
from django.db import connection, connections
from django.test import TestCase, TransactionTestCase

from apps.core.models import AuditEvent
from apps.pharmacy import purchasing, stock
from apps.pharmacy.forms import PurchaseItemForm
from apps.pharmacy.models import (InventoryTransaction, MedicineBatch, PurchaseInvoice, PurchaseInvoiceItem,
                                  PurchaseLockedError, Supplier)

from .helpers import Shop


class PurchaseBase(TestCase):
    def setUp(self):
        self.s = Shop()
        self.med = self.s.medicine(pack_size=10)

    def draft(self, number="INV-1", supplier=None, **kw):
        return purchasing.create_draft(actor=self.s.pharm, request=None, supplier=supplier or self.s.supplier,
                                       supplier_invoice_number=number, invoice_date=self.s.today, **kw)

    def item(self, inv, **over):
        data = dict(medicine=self.med, batch_number="B1", expiry_date=self.s.today + timedelta(days=365), packs=5,
                    units_per_pack=10, purchase_price_per_pack=Decimal("50.00"), mrp_per_pack=Decimal("60.00"))
        data.update(over)
        return purchasing.add_item(actor=self.s.pharm, request=None, invoice_id=inv.pk, **data)


class DraftAndReceiveTests(PurchaseBase):
    def test_item_computes_units_and_line_total(self):
        i = self.item(self.draft())
        self.assertEqual((i.quantity_units, i.line_total), (50, Decimal("250.00")))

    def test_receive_creates_batches_ledger_numbers_and_totals(self):
        inv = self.draft(discount_amount=Decimal("10"), tax_amount=Decimal("20"))
        self.item(inv)
        self.item(inv, batch_number="B2", packs=2, purchase_price_per_pack=Decimal("40"), mrp_per_pack=Decimal("55"))
        done = purchasing.receive(actor=self.s.pharm, request=None, invoice_id=inv.pk)
        self.assertEqual((done.status, done.number), ("RECEIVED", f"PU-{self.s.today.year}-000001"))
        self.assertEqual((done.subtotal, done.grand_total), (Decimal("330.00"), Decimal("340.00")))
        self.assertEqual((done.received_by, done.supplier), (self.s.pharm, self.s.supplier))
        b1, b2 = MedicineBatch.objects.order_by("batch_number")
        self.assertEqual((b1.quantity_on_hand, b1.purchase_price_per_unit, b1.selling_price_per_unit),
                         (50, Decimal("5.0000"), Decimal("6.0000")))
        self.assertEqual((b2.quantity_on_hand, b2.purchase_price_per_unit, b2.selling_price_per_unit),
                         (20, Decimal("4.0000"), Decimal("5.5000")))
        self.assertEqual((b1.supplier, b1.created_by), (self.s.supplier, self.s.pharm))
        rows = list(InventoryTransaction.objects.order_by("id"))
        self.assertEqual([(r.kind, r.quantity_change, r.balance_after, r.reference_type, r.reference_id) for r in rows],
                         [("RECEIPT", 50, 50, "purchase_invoice", str(inv.pk)), ("RECEIPT", 20, 20, "purchase_invoice", str(inv.pk))])
        self.assertEqual(stock.reconcile(), [])
        row = next(r for r in stock.medicine_stock_rows() if r["medicine"] == self.med)
        self.assertEqual(row["usable"], 70)
        self.assertTrue(AuditEvent.objects.filter(action="purchase.receive").exists())

    def test_numbers_increase_per_received_purchase(self):
        a, b = self.draft("A"), self.draft("B")
        self.item(a)
        self.item(b, batch_number="B9")
        self.assertEqual(purchasing.receive(actor=self.s.pharm, request=None, invoice_id=a.pk).number[-6:], "000001")
        self.assertEqual(purchasing.receive(actor=self.s.pharm, request=None, invoice_id=b.pk).number[-6:], "000002")

    def test_expired_or_same_day_expiry_refused_at_entry(self):
        inv = self.draft()
        for days in (-30, 0):
            with self.assertRaises(purchasing.PurchaseError):
                self.item(inv, expiry_date=self.s.today + timedelta(days=days))
        self.assertEqual(inv.items.count(), 0)

    def test_receive_rechecks_expiry_if_days_passed_since_entry(self):
        inv = self.draft()
        self.item(inv, expiry_date=self.s.today + timedelta(days=1))
        with self.assertRaises(purchasing.PurchaseError):
            purchasing.receive(actor=self.s.pharm, request=None, invoice_id=inv.pk, today=self.s.today + timedelta(days=2))
        inv.refresh_from_db()
        self.assertEqual(inv.status, "DRAFT")
        self.assertEqual(MedicineBatch.objects.count(), 0)

    def test_duplicate_supplier_invoice_number_refused_but_other_supplier_allowed(self):
        self.draft("SAME")
        with self.assertRaises(purchasing.PurchaseError):
            self.draft("SAME")
        other = Supplier.objects.create(name="Other Supplier")
        self.assertEqual(self.draft("SAME", supplier=other).supplier, other)

    def test_inactive_supplier_or_medicine_refused(self):
        self.s.supplier.is_active = False
        self.s.supplier.save()
        with self.assertRaises(purchasing.PurchaseError):
            self.draft()
        self.s.supplier.is_active = True
        self.s.supplier.save()
        inv = self.draft()
        self.med.is_active = False
        self.med.save()
        with self.assertRaises(purchasing.PurchaseError):
            self.item(inv)

    def test_draft_can_be_edited_and_discarded(self):
        inv = self.draft()
        item = self.item(inv)
        purchasing.update_header(actor=self.s.pharm, request=None, invoice_id=inv.pk, discount_amount=Decimal("5"))
        inv.refresh_from_db()
        self.assertEqual(inv.discount_amount, Decimal("5.00"))
        purchasing.remove_item(actor=self.s.pharm, request=None, item_id=item.pk)
        self.assertEqual(PurchaseInvoiceItem.objects.count(), 0)
        purchasing.discard_draft(actor=self.s.pharm, request=None, invoice_id=inv.pk)
        self.assertEqual(PurchaseInvoice.objects.count(), 0)

    def test_empty_purchase_cannot_be_received(self):
        with self.assertRaises(purchasing.PurchaseError):
            purchasing.receive(actor=self.s.pharm, request=None, invoice_id=self.draft().pk)

    def test_only_pharmacy_and_admin_may_purchase(self):
        for user in (self.s.doctor, self.s.reception):
            with self.assertRaises(PermissionDenied):
                purchasing.create_draft(actor=user, request=None, supplier=self.s.supplier, supplier_invoice_number="X",
                                        invoice_date=self.s.today)
        self.assertEqual(purchasing.create_draft(actor=self.s.admin, request=None, supplier=self.s.supplier,
                                                 supplier_invoice_number="ADM", invoice_date=self.s.today).created_by, self.s.admin)


class ReceivedPurchaseIsLockedTests(PurchaseBase):
    def setUp(self):
        super().setUp()
        self.inv = self.draft()
        self.line = self.item(self.inv)
        self.inv = purchasing.receive(actor=self.s.pharm, request=None, invoice_id=self.inv.pk)

    def test_second_receive_does_not_double_stock(self):
        with self.assertRaises(purchasing.PurchaseError):
            purchasing.receive(actor=self.s.pharm, request=None, invoice_id=self.inv.pk)
        self.assertEqual(MedicineBatch.objects.get().quantity_on_hand, 50)
        self.assertEqual(InventoryTransaction.objects.count(), 1)

    def test_services_refuse_changes(self):
        with self.assertRaises(purchasing.PurchaseError):
            self.item(self.inv, batch_number="LATE")
        with self.assertRaises(purchasing.PurchaseError):
            purchasing.remove_item(actor=self.s.pharm, request=None, item_id=self.line.pk)
        with self.assertRaises(purchasing.PurchaseError):
            purchasing.update_header(actor=self.s.pharm, request=None, invoice_id=self.inv.pk, tax_amount=Decimal("1"))
        with self.assertRaises(purchasing.PurchaseError):
            purchasing.discard_draft(actor=self.s.pharm, request=None, invoice_id=self.inv.pk)

    def test_model_layer_refuses_even_if_a_service_were_bypassed(self):
        inv = PurchaseInvoice.objects.get(pk=self.inv.pk)
        inv.discount_amount = Decimal("999")
        with self.assertRaises(PurchaseLockedError):
            inv.save()
        with self.assertRaises(PurchaseLockedError):
            PurchaseInvoice.objects.get(pk=self.inv.pk).delete()
        line = PurchaseInvoiceItem.objects.get(pk=self.line.pk)
        line.packs = 999
        with self.assertRaises(PurchaseLockedError):
            line.save()
        with self.assertRaises(PurchaseLockedError):
            line.delete()
        self.assertEqual(PurchaseInvoiceItem.objects.get(pk=self.line.pk).packs, 5)


class ItemFormTests(PurchaseBase):
    base = {"batch_number": "B1", "packs": "2", "purchase_price_per_pack": "10", "mrp_per_pack": "12"}

    def test_units_per_pack_defaults_to_catalogue_pack_size(self):
        exp = (self.s.today + timedelta(days=100)).isoformat()
        f = PurchaseItemForm({**self.base, "medicine": self.med.pk, "expiry_date": exp})
        self.assertTrue(f.is_valid(), f.errors)
        self.assertEqual(f.cleaned_data["units_per_pack"], 10)

    def test_past_expiry_rejected_and_zero_packs_rejected(self):
        past = (self.s.today - timedelta(days=1)).isoformat()
        f = PurchaseItemForm({**self.base, "medicine": self.med.pk, "expiry_date": past})
        self.assertIn("has passed", str(f.errors))
        good = (self.s.today + timedelta(days=100)).isoformat()
        self.assertFalse(PurchaseItemForm({**self.base, "packs": "0", "medicine": self.med.pk, "expiry_date": good}).is_valid())


@unittest.skipUnless(connection.vendor == "postgresql", "Concurrency tests need PostgreSQL")
class ConcurrentReceiveTests(TransactionTestCase):
    def test_two_workstations_receiving_the_same_purchase_add_stock_once(self):
        s = Shop()
        med = s.medicine()
        inv = purchasing.create_draft(actor=s.pharm, request=None, supplier=s.supplier, supplier_invoice_number="C1",
                                      invoice_date=s.today)
        purchasing.add_item(actor=s.pharm, request=None, invoice_id=inv.pk, medicine=med, batch_number="B1",
                            expiry_date=s.today + timedelta(days=200), packs=5, units_per_pack=10,
                            purchase_price_per_pack=Decimal("50"), mrp_per_pack=Decimal("60"))
        results, errors = [], []
        barrier = threading.Barrier(3)

        def worker():
            try:
                barrier.wait()
                results.append(purchasing.receive(actor=s.pharm, request=None, invoice_id=inv.pk))
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)
            finally:
                connections.close_all()

        threads = [threading.Thread(target=worker) for _ in range(3)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        self.assertEqual((len(results), len(errors)), (1, 2))
        self.assertTrue(all(isinstance(e, purchasing.PurchaseError) for e in errors), errors)
        self.assertEqual(MedicineBatch.objects.get().quantity_on_hand, 50)
        self.assertEqual(stock.reconcile(), [])
