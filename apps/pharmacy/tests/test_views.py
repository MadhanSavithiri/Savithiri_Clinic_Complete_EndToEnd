from datetime import timedelta
from decimal import Decimal

from django.contrib.messages import get_messages
from django.test import TestCase
from django.urls import reverse

from apps.pharmacy import purchasing, stock
from apps.pharmacy.models import MedicineBatch, PurchaseInvoice, Supplier

from .helpers import Shop


def msgs(r):
    return [str(m) for m in get_messages(r.wsgi_request)]


class Base(TestCase):
    def setUp(self):
        self.s = Shop()
        self.med = self.s.medicine("Paramol", "500 mg", reorder_threshold=500)
        self.batch = self.s.batch(self.med, qty=100, days=365, number="B1", cost="2", sell="3")
        self.client.force_login(self.s.pharm)

    def as_(self, user):
        self.client.force_login(user)


class AccessTests(Base):
    def test_stock_views_for_pharmacy_admin_doctor_but_not_reception(self):
        urls = [reverse(n) for n in ("pharmacy:stock_overview", "pharmacy:batch_report", "pharmacy:expiry_report", "pharmacy:ledger")]
        urls += [reverse("pharmacy:batch_detail", args=[self.batch.pk]), reverse("pharmacy:medicine_detail", args=[self.med.pk])]
        for user, code in ((self.s.pharm, 200), (self.s.admin, 200), (self.s.doctor, 200), (self.s.reception, 403)):
            self.as_(user)
            for url in urls:
                self.assertEqual(self.client.get(url).status_code, code, f"{user.role} {url}")
        self.client.logout()
        self.assertEqual(self.client.get(urls[0]).status_code, 302)

    def test_cost_pages_are_for_pharmacy_and_admin_only(self):
        urls = [reverse("pharmacy:valuation"), reverse("pharmacy:purchase_report")]
        for user, code in ((self.s.pharm, 200), (self.s.admin, 200), (self.s.doctor, 403), (self.s.reception, 403)):
            self.as_(user)
            for url in urls:
                self.assertEqual(self.client.get(url).status_code, code, f"{user.role} {url}")

    def test_purchasing_and_suppliers_for_pharmacy_and_admin_only(self):
        urls = [reverse(n) for n in ("pharmacy:purchase_list", "pharmacy:purchase_create", "pharmacy:supplier_list", "pharmacy:supplier_create")]
        for user, code in ((self.s.pharm, 200), (self.s.admin, 200), (self.s.doctor, 403), (self.s.reception, 403)):
            self.as_(user)
            for url in urls:
                self.assertEqual(self.client.get(url).status_code, code, f"{user.role} {url}")

    def test_doctor_and_reception_cannot_change_stock(self):
        for user in (self.s.doctor, self.s.reception):
            self.as_(user)
            for name in ("batch_adjust", "batch_wastage", "batch_return", "batch_recall", "batch_unrecall"):
                r = self.client.post(reverse(f"pharmacy:{name}", args=[self.batch.pk]),
                                     {"direction": "remove", "quantity": "5", "reason": "x"})
                self.assertEqual(r.status_code, 403, f"{user.role} {name}")
        self.batch.refresh_from_db()
        self.assertEqual(self.batch.quantity_on_hand, 100)


class CostVisibilityTests(Base):
    def test_doctor_sees_availability_but_no_purchase_prices(self):
        self.as_(self.s.doctor)
        page = self.client.get(reverse("pharmacy:batch_report"))
        self.assertContains(page, "Sell / unit")
        self.assertNotContains(page, "Cost / unit")
        self.assertNotContains(self.client.get(reverse("pharmacy:batch_detail", args=[self.batch.pk])), "Cost / unit")
        self.assertNotContains(self.client.get(reverse("pharmacy:medicine_detail", args=[self.med.pk])), "Cost / unit")
        self.as_(self.s.pharm)
        self.assertContains(self.client.get(reverse("pharmacy:batch_report")), "Cost / unit")

    def test_csv_omits_costs_for_the_doctor(self):
        self.as_(self.s.doctor)
        text = self.client.get(reverse("pharmacy:batch_report"), {"fmt": "csv"}).content.decode("utf-8-sig")
        self.assertNotIn("Cost per unit", text)
        self.assertNotIn("2.0000", text)
        self.as_(self.s.pharm)
        text = self.client.get(reverse("pharmacy:batch_report"), {"fmt": "csv"}).content.decode("utf-8-sig")
        self.assertIn("Cost per unit", text)
        self.assertIn("2.0000", text)

    def test_csv_is_safe_against_spreadsheet_formulas(self):
        evil = self.s.medicine("=HYPERLINK(1)", "")
        self.s.batch(evil, qty=3, number="@bad")
        r = self.client.get(reverse("pharmacy:batch_report"), {"fmt": "csv"})
        text = r.content.decode("utf-8-sig")
        self.assertIn("'=HYPERLINK(1)", text)
        self.assertIn("'@bad", text)
        self.assertIn("attachment", r["Content-Disposition"])


class PurchaseScreenTests(Base):
    def test_full_flow_supplier_to_stock(self):
        r = self.client.post(reverse("pharmacy:supplier_create"), {"name": "Alpha Pharma", "is_active": "on"})
        self.assertEqual(r.status_code, 302)
        sup = Supplier.objects.get(name="Alpha Pharma")
        r = self.client.post(reverse("pharmacy:purchase_create"), {
            "supplier": sup.pk, "supplier_invoice_number": "A-1", "invoice_date": self.s.today.isoformat(),
            "discount_amount": "0", "tax_amount": "12.50", "notes": ""})
        inv = PurchaseInvoice.objects.get(supplier_invoice_number="A-1")
        self.assertRedirects(r, reverse("pharmacy:purchase_detail", args=[inv.pk]), fetch_redirect_response=False)
        self.client.post(reverse("pharmacy:purchase_item_add", args=[inv.pk]), {
            "medicine": self.med.pk, "batch_number": "NEW1", "expiry_date": (self.s.today + timedelta(days=400)).isoformat(),
            "packs": "5", "units_per_pack": "", "purchase_price_per_pack": "50", "mrp_per_pack": "60"})
        page = self.client.get(reverse("pharmacy:purchase_detail", args=[inv.pk]))
        self.assertContains(page, "NEW1")
        self.assertContains(page, "Nothing is in stock until")
        before = stock.medicine_stock_rows()[0]["usable"]

        r = self.client.post(reverse("pharmacy:purchase_receive", args=[inv.pk]))
        self.assertIn("Received into stock as PU-", msgs(r)[-1])
        self.assertEqual(stock.medicine_stock_rows()[0]["usable"], before + 50)
        self.assertEqual(MedicineBatch.objects.get(batch_number="NEW1").quantity_on_hand, 50)
        again = self.client.post(reverse("pharmacy:purchase_receive", args=[inv.pk]))
        self.assertIn("already been received", msgs(again)[-1])
        self.assertEqual(MedicineBatch.objects.get(batch_number="NEW1").quantity_on_hand, 50)
        self.assertNotContains(self.client.get(reverse("pharmacy:purchase_detail", args=[inv.pk])), "Add an item")

    def test_expired_item_rejected_on_the_screen(self):
        inv = purchasing.create_draft(actor=self.s.pharm, request=None, supplier=self.s.supplier,
                                      supplier_invoice_number="Z", invoice_date=self.s.today)
        r = self.client.post(reverse("pharmacy:purchase_item_add", args=[inv.pk]), {
            "medicine": self.med.pk, "batch_number": "OLD", "expiry_date": (self.s.today - timedelta(days=5)).isoformat(),
            "packs": "1", "units_per_pack": "10", "purchase_price_per_pack": "5", "mrp_per_pack": "6"})
        self.assertContains(r, "has passed")
        self.assertEqual(inv.items.count(), 0)

    def test_duplicate_invoice_number_is_refused_on_the_screen(self):
        data = {"supplier": self.s.supplier.pk, "supplier_invoice_number": "DUP", "invoice_date": self.s.today.isoformat(),
                "discount_amount": "0", "tax_amount": "0", "notes": ""}
        self.client.post(reverse("pharmacy:purchase_create"), data)
        r = self.client.post(reverse("pharmacy:purchase_create"), data)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(PurchaseInvoice.objects.count(), 1)

    def test_received_purchase_cannot_be_edited_on_the_screen(self):
        inv = purchasing.create_draft(actor=self.s.pharm, request=None, supplier=self.s.supplier,
                                      supplier_invoice_number="E", invoice_date=self.s.today)
        purchasing.add_item(actor=self.s.pharm, request=None, invoice_id=inv.pk, medicine=self.med, batch_number="X",
                            expiry_date=self.s.today + timedelta(days=90), packs=1, units_per_pack=10,
                            purchase_price_per_pack=Decimal("5"), mrp_per_pack=Decimal("6"))
        purchasing.receive(actor=self.s.pharm, request=None, invoice_id=inv.pk)
        r = self.client.get(reverse("pharmacy:purchase_edit", args=[inv.pk]))
        self.assertEqual(r.status_code, 302)
        r = self.client.post(reverse("pharmacy:purchase_discard", args=[inv.pk]))
        self.assertIn("Only a draft", msgs(r)[-1])
        self.assertEqual(PurchaseInvoice.objects.count(), 1)

    def test_purchase_report_totals_and_csv(self):
        for number, price in (("R1", "50"), ("R2", "30")):
            inv = purchasing.create_draft(actor=self.s.pharm, request=None, supplier=self.s.supplier,
                                          supplier_invoice_number=number, invoice_date=self.s.today)
            purchasing.add_item(actor=self.s.pharm, request=None, invoice_id=inv.pk, medicine=self.med, batch_number=number,
                                expiry_date=self.s.today + timedelta(days=90), packs=2, units_per_pack=10,
                                purchase_price_per_pack=Decimal(price), mrp_per_pack=Decimal("60"))
            purchasing.receive(actor=self.s.pharm, request=None, invoice_id=inv.pk)
        page = self.client.get(reverse("pharmacy:purchase_report"))
        self.assertContains(page, "160.00")  # 2*50 + 2*30
        csv = self.client.get(reverse("pharmacy:purchase_report"), {"fmt": "csv"}).content.decode("utf-8-sig")
        self.assertIn("R1", csv)
        self.assertIn("100.00", csv)
        none = self.client.get(reverse("pharmacy:purchase_report"), {"from": (self.s.today + timedelta(days=1)).isoformat()})
        self.assertContains(none, "No purchases in this period")


class BatchScreenTests(Base):
    def post(self, name, **data):
        return self.client.post(reverse(f"pharmacy:{name}", args=[self.batch.pk]), data)

    def test_adjust_wastage_return_and_limits(self):
        self.post("batch_adjust", direction="remove", quantity="3", reason="Count was off")
        self.post("batch_wastage", quantity="7", reason="Broken")
        self.post("batch_return", quantity="10", reason="Wrong strength")
        self.batch.refresh_from_db()
        self.assertEqual(self.batch.quantity_on_hand, 80)
        r = self.post("batch_wastage", quantity="500", reason="too many")
        self.assertIn("Only 80", msgs(r)[-1])
        r = self.post("batch_adjust", direction="add", quantity="2", reason="")
        self.batch.refresh_from_db()
        self.assertEqual(self.batch.quantity_on_hand, 80)
        page = self.client.get(reverse("pharmacy:batch_detail", args=[self.batch.pk]))
        for text in ("Count was off", "Broken", "Wrong strength"):
            self.assertContains(page, text)
        self.assertEqual(stock.reconcile(), [])

    def test_recall_blocks_dispensing_and_shows_on_screens(self):
        self.post("batch_recall", reason="Manufacturer notice")
        self.assertContains(self.client.get(reverse("pharmacy:batch_detail", args=[self.batch.pk])), "Recalled")
        row = stock.medicine_stock_rows()[0]
        self.assertEqual((row["usable"], row["recalled"]), (0, 100))
        with self.assertRaises(stock.StockError):
            stock.consume_stock(actor=self.s.pharm, request=None, medicine_id=self.med.pk, quantity=1)
        self.post("batch_unrecall")
        self.assertEqual(stock.medicine_stock_rows()[0]["usable"], 100)

    def test_admin_may_adjust_and_return(self):
        self.as_(self.s.admin)
        self.post("batch_adjust", direction="remove", quantity="1", reason="admin count")
        self.post("batch_return", quantity="1", reason="admin return")
        self.batch.refresh_from_db()
        self.assertEqual(self.batch.quantity_on_hand, 98)
        page = self.client.get(reverse("pharmacy:batch_detail", args=[self.batch.pk]))
        self.assertContains(page, "Stock-take correction")


class ReportScreenTests(Base):
    def test_overview_filters_and_flags(self):
        near = self.s.medicine("NearOne", "")
        self.s.batch(near, qty=5, days=20, number="N1")
        r = self.client.get(reverse("pharmacy:stock_overview"), {"show": "near"})
        self.assertContains(r, "NearOne")
        self.assertNotContains(r, "Paramol")
        low = self.client.get(reverse("pharmacy:stock_overview"), {"show": "low"})
        self.assertContains(low, "Paramol")  # 100 usable, reorder at 500
        self.assertContains(low, "Low")

    def test_expiry_report_lists_expired_and_near(self):
        self.s.batch(self.s.medicine("Gone", ""), qty=4, days=-10, number="X1")
        self.s.batch(self.s.medicine("Soon", ""), qty=4, days=15, number="X2")
        page = self.client.get(reverse("pharmacy:expiry_report"))
        self.assertContains(page, "Gone")
        self.assertContains(page, "Soon")
        self.assertNotContains(page, "Paramol")
        wide = self.client.get(reverse("pharmacy:expiry_report"), {"days": 1000})
        self.assertContains(wide, "Paramol")
        self.assertIn("Days left", self.client.get(reverse("pharmacy:expiry_report"), {"fmt": "csv"}).content.decode("utf-8-sig"))

    def test_ledger_filters_and_csv(self):
        stock.record_wastage(actor=self.s.pharm, request=None, batch_id=self.batch.pk, quantity=2, reason="Spilled")
        kinds = lambda r: {t.kind for t in r.context["rows"]}  # noqa: E731  (the page also lists every type in a dropdown)
        all_rows = self.client.get(reverse("pharmacy:ledger"))
        self.assertEqual(kinds(all_rows), {"RECEIPT", "WASTAGE"})
        self.assertContains(all_rows, "Spilled")
        only = self.client.get(reverse("pharmacy:ledger"), {"kind": "WASTAGE"})
        self.assertEqual(kinds(only), {"WASTAGE"})
        self.assertContains(only, "Spilled")
        future = self.client.get(reverse("pharmacy:ledger"), {"from": (self.s.today + timedelta(days=1)).isoformat()})
        self.assertEqual(list(future.context["rows"]), [])
        self.assertContains(future, "No movements match")
        today = self.client.get(reverse("pharmacy:ledger"), {"from": self.s.today.isoformat(), "to": self.s.today.isoformat()})
        self.assertEqual(kinds(today), {"RECEIPT", "WASTAGE"})
        csv = self.client.get(reverse("pharmacy:ledger"), {"fmt": "csv"}).content.decode("utf-8-sig")
        self.assertIn("Spilled", csv)

    def test_valuation_page(self):
        page = self.client.get(reverse("pharmacy:valuation"))
        self.assertContains(page, "200.00")  # 100 units x 2.00 cost
        self.assertContains(page, "300.00")  # 100 units x 3.00 selling price
        self.assertIn("Value at cost", self.client.get(reverse("pharmacy:valuation"), {"fmt": "csv"}).content.decode("utf-8-sig"))


class HomeAndNavTests(Base):
    def test_cards_follow_permissions(self):
        for user, shows, hides in ((self.s.doctor, ["Stock and expiry"], ["Purchases"]),
                                   (self.s.pharm, ["Stock and expiry", "Purchases"], []),
                                   (self.s.admin, ["Stock and expiry", "Purchases"], []),
                                   (self.s.reception, [], ["Stock and expiry", "Purchases"])):
            self.as_(user)
            page = self.client.get(reverse("home"))
            for t in shows:
                self.assertContains(page, t, msg_prefix=user.role)
            for t in hides:
                self.assertNotContains(page, t, msg_prefix=user.role)
