from datetime import timedelta
from decimal import Decimal

from django.utils import timezone

from apps.accounts.tests.helpers import make_user
from apps.pharmacy.models import InventoryTransaction, Medicine, MedicineBatch, Supplier


class Shop:
    """Users plus helpers to build medicines and stock quickly."""

    def __init__(self):
        self.pharm = make_user("pharm", "pharmacy_manager")      # catalogue, purchases, stock, costs
        self.clinic_pharm = make_user("cpharm", "pharmacy")      # dispenses the doctor's prescriptions
        self.counter = make_user("counter1", "counter")          # independent counter salesperson
        self.admin = make_user("boss", "admin")
        self.doctor = make_user("doc", "doctor")
        self.reception = make_user("rita", "reception")
        self.supplier = Supplier.objects.create(name="Test Supplier Co")
        self.today = timezone.localdate()

    def medicine(self, name="TestBrand", strength="250 mg", **kw):
        data = {"brand_name": name, "strength": strength, "dosage_form": "Tablet", "pack_size": 10}
        data.update(kw)
        return Medicine.objects.create(**data)

    def batch(self, medicine, qty=100, days=365, number="B1", cost="2", sell="3", received=None, recalled=False):
        b = MedicineBatch.objects.create(
            medicine=medicine, supplier=self.supplier, batch_number=number, expiry_date=self.today + timedelta(days=days),
            received_date=received or self.today, quantity_received=qty, quantity_on_hand=qty,
            purchase_price_per_unit=Decimal(cost), selling_price_per_unit=Decimal(sell), is_recalled=recalled)
        InventoryTransaction.objects.create(
            batch=b, medicine=medicine, kind="RECEIPT", quantity_change=qty, balance_after=qty,
            unit_cost=b.purchase_price_per_unit, created_by=self.pharm)
        return b
