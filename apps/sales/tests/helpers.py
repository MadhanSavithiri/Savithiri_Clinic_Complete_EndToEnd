from datetime import timedelta
from decimal import Decimal

from django.utils import timezone

from apps.accounts.tests.helpers import PW
from apps.pharmacy.tests.helpers import Shop
from apps.sales import search, services


class CounterShop(Shop):
    """The pharmacy test shop plus a counter salesperson with an open shift and 100 units of Paracetamol."""

    def __init__(self):
        super().__init__()
        self.med = self.medicine("Paracetamol", "500 mg", rack="R02", shelf="S03", box="B12", generic_name="paracetamol")
        self.stock_batch = self.batch(self.med, qty=100, days=365, number="PB1", cost="1.5", sell="2")
        self.shift = services.open_shift(actor=self.counter, request=None, opening_cash=Decimal("500"))

    def open_shift_for(self, user, cash="0"):
        return services.open_shift(actor=user, request=None, opening_cash=Decimal(cash))

    def cart(self, med=None, qty=1, user=None):
        user = user or self.counter
        services.add_to_cart(actor=user, request=None, medicine_id=(med or self.med).pk, quantity=qty)
        return services.get_draft(user)

    def total_of(self, draft):
        return search.cart_context(draft)["subtotal"]

    def pay(self, total, method="CASH", **extra):
        return [{"method": method, "amount": total, "reference": "", "confirmed": method in ("UPI", "CARD"), **extra}]

    def sell(self, qty=1, med=None, user=None, **kw):
        user = user or self.counter
        draft = self.cart(med, qty, user)
        total = kw.pop("total", None)
        if total is None:
            pct = Decimal(str(kw.get("discount_percent", 0)))
            sub = self.total_of(draft)
            discount = sum(((p["line_total"] * pct / 100).quantize(Decimal("0.01"), rounding="ROUND_HALF_UP")
                            for r in search.cart_context(draft)["rows"] for p in r["parts"]), Decimal("0"))
            total = sub - discount
        payments = kw.pop("payments", None) or self.pay(total)
        return services.complete_counter_sale(actor=user, request=None, sale_id=draft.pk, payments=payments, **kw)

    def restricted_data(self, **over):
        data = {"prescriber_name": "Dr Test Prescriber", "prescriber_reg_no": "TEST-REG-77", "patient_name": "Walk In Patient",
                "approver_username": "cpharm", "approver_password": PW}
        data.update(over)
        return data
