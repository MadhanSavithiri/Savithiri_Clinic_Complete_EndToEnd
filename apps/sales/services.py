"""Counter-sale rules. A bill is an open draft until it is COMPLETED in ONE database transaction:
re-check stock, take stock (FEFO, row-locked), price it, check the payments, number the bill. If anything
fails, nothing is deducted. Completing an already completed bill returns it unchanged (safe retries)."""
from decimal import Decimal, ROUND_HALF_UP

from django.contrib.auth import authenticate
from django.core.exceptions import PermissionDenied
from django.db import IntegrityError, transaction
from django.db.models import Sum
from django.utils import timezone

from apps.accounts.models import User
from apps.accounts.permissions import user_has_permission
from apps.core.audit import log_event
from apps.core.models import DocType, SalesChannel, Setting
from apps.core.sequences import next_number
from apps.pharmacy import stock
from apps.pharmacy.models import Medicine

from .models import Desk, Payment, PaymentMethod, Sale, SaleLine, Shift, ZERO
from .rules import BLOCKED_CATEGORIES, needs_approval

COUNTER = SalesChannel.INDEPENDENT_COUNTER
SELL = "sales.counter_sell"


class SaleError(Exception):
    """A rule was broken; the message is safe to show to staff."""


def _require(actor, code):
    if not user_has_permission(actor, code):
        raise PermissionDenied


def money(value):
    return Decimal(value).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def enabled_methods():
    configured = Setting.get("enabled_payment_methods", None)
    allowed = [m for m in (configured or []) if m in PaymentMethod.values]
    return allowed or list(PaymentMethod.values)


def discount_limit(user):
    manager = user.role == User.Role.PHARMACY_MANAGER
    key, default = ("manager_discount_limit_percent", 10) if manager else ("counter_discount_limit_percent", 5)
    try:
        return Decimal(str(Setting.get(key, default)))
    except Exception:  # noqa: BLE001
        return Decimal(default)


# ---------------------------------------------------------------- shifts
def current_shift(user, desk=Desk.COUNTER):
    return Shift.objects.filter(user=user, desk=desk, closed_at__isnull=True).first()


def open_shift(*, actor, request, opening_cash):
    _require(actor, SELL)
    cash = money(opening_cash)
    if cash < 0:
        raise SaleError("Opening cash cannot be negative.")
    try:
        with transaction.atomic():
            shift = Shift.objects.create(user=actor, desk=Desk.COUNTER, opening_cash=cash)
            log_event(user=actor, action="shift.open", obj=shift, request=request)
    except IntegrityError:
        raise SaleError("You already have an open shift.")
    return shift


def shift_summary(shift):
    """Money taken in this shift by method, bills made, and the cash that should be in the drawer."""
    totals = {m: {"in": ZERO, "out": ZERO} for m in PaymentMethod.values}
    for row in Payment.objects.filter(shift=shift).values("method", "kind").annotate(total=Sum("amount")):
        totals[row["method"]]["in" if row["kind"] == Payment.Kind.PAYMENT else "out"] = row["total"]
    sales = Sale.objects.filter(shift=shift, status=Sale.Status.COMPLETED)
    cash_in, cash_out = totals[PaymentMethod.CASH]["in"], totals[PaymentMethod.CASH]["out"]
    return {
        "by_method": [{"method": m, "label": PaymentMethod(m).label, "in": v["in"], "out": v["out"], "net": v["in"] - v["out"]}
                      for m, v in totals.items()],
        "bills": sales.count(),
        "sales_total": sales.aggregate(t=Sum("total"))["t"] or ZERO,
        "collected": sum((v["in"] - v["out"] for v in totals.values()), ZERO),
        "expected_cash": shift.opening_cash + cash_in - cash_out,
    }


def close_shift(*, actor, request, shift_id, counted_cash, notes=""):
    _require(actor, SELL)
    counted = money(counted_cash)
    if counted < 0:
        raise SaleError("Counted cash cannot be negative.")
    with transaction.atomic():
        shift = Shift.objects.select_for_update().get(pk=shift_id)
        if shift.user_id != actor.pk:
            raise PermissionDenied
        if shift.closed_at is not None:
            raise SaleError("This shift is already closed.")
        if Sale.objects.filter(cashier=actor, channel=COUNTER, status=Sale.Status.DRAFT, lines__isnull=False).exists():
            raise SaleError("There is an open bill with items. Complete it or discard it before closing the shift.")
        summary = shift_summary(shift)
        shift.expected_cash, shift.counted_cash = summary["expected_cash"], counted
        shift.variance = counted - summary["expected_cash"]
        shift.closed_at, shift.close_notes = timezone.now(), (notes or "").strip()
        shift.save()
        Sale.objects.filter(cashier=actor, channel=COUNTER, status=Sale.Status.DRAFT, lines__isnull=True).delete()
        log_event(user=actor, action="shift.close", obj=shift, summary=f"Variance {shift.variance}", request=request)
    return shift


# ---------------------------------------------------------------- the open bill (cart)
def get_draft(user):
    return Sale.objects.filter(cashier=user, channel=COUNTER, status=Sale.Status.DRAFT).first()


def _get_or_create_draft(actor):
    draft = get_draft(actor)
    if draft:
        return draft
    try:
        with transaction.atomic():
            return Sale.objects.create(channel=COUNTER, cashier=actor)
    except IntegrityError:
        return get_draft(actor)


def _describe(med):
    return " ".join(p for p in [med.brand_name, med.strength, med.dosage_form] if p)


def _check_sellable(med):
    if not med.is_active:
        raise SaleError(f"{med.brand_name} is not in the active catalogue.")
    if not med.counter_sale_allowed:
        raise SaleError(f"{med.brand_name} is not sold at the counter.")
    if med.schedule_category in BLOCKED_CATEGORIES and not Setting.get("counter_allow_schedule_x", False):
        raise SaleError(f"{med.brand_name} is a Schedule X medicine and cannot be sold at the counter.")


def add_to_cart(*, actor, request, medicine_id, quantity):
    _require(actor, SELL)
    qty = int(quantity)
    if qty < 1:
        raise SaleError("Quantity must be at least 1.")
    if current_shift(actor) is None:
        raise SaleError("Open your shift before billing.")
    med = Medicine.objects.filter(pk=medicine_id).first()
    if med is None:
        raise SaleError("That item was not found.")
    _check_sellable(med)
    with transaction.atomic():
        draft = _get_or_create_draft(actor)
        line = draft.lines.filter(medicine=med).first()
        wanted = (line.quantity if line else 0) + qty
        available = stock.available_units(med.pk)
        if wanted > available:
            raise SaleError(f"Only {available} {med.unit}(s) of {med.brand_name} can be sold right now "
                            "(expired and recalled stock is not counted).")
        if line:
            line.quantity = wanted
            line.save()
        else:
            line = SaleLine.objects.create(sale=draft, medicine=med, quantity=qty, description=_describe(med))
    return line


def set_quantity(*, actor, request, line_id, quantity):
    _require(actor, SELL)
    qty = int(quantity)
    line = SaleLine.objects.select_related("sale", "medicine").filter(pk=line_id).first()
    if line is None:
        raise SaleError("That line is no longer on the bill.")
    if line.sale.cashier_id != actor.pk or line.sale.status != Sale.Status.DRAFT:
        raise PermissionDenied
    if qty <= 0:
        line.delete()
        return None
    available = stock.available_units(line.medicine_id)
    if qty > available:
        raise SaleError(f"Only {available} {line.medicine.unit}(s) can be sold right now.")
    line.quantity = qty
    line.save()
    return line


def remove_line(*, actor, request, line_id):
    return set_quantity(actor=actor, request=request, line_id=line_id, quantity=0)


def discard_draft(*, actor, request):
    _require(actor, SELL)
    draft = get_draft(actor)
    if draft is not None:
        with transaction.atomic():
            draft.delete()
            log_event(user=actor, action="sale.discard_open_bill", request=request)


# ---------------------------------------------------------------- checkout
def _verify_restricted(request, data):
    data = data or {}
    wanted = ("prescriber_name", "prescriber_reg_no", "patient_name", "approver_username", "approver_password")
    if not all((data.get(k) or "").strip() for k in wanted):
        raise SaleError("This bill has prescription-only medicines. Enter the prescriber's name and registration "
                        "number, the patient's name, and the pharmacist's username and password to approve it.")
    approver = authenticate(request, username=data["approver_username"].strip(), password=data["approver_password"])
    if approver is None:
        raise SaleError("Pharmacist approval failed: wrong username or password.")
    if not user_has_permission(approver, "sales.approve_restricted"):
        raise SaleError("That user is not allowed to approve prescription-only sales.")
    return approver


def _validate_discount(actor, percent, reason):
    pct = Decimal(str(percent or 0))
    if pct == 0:
        return Decimal("0.00")
    if pct < 0 or pct > 100:
        raise SaleError("The discount must be between 0 and 100 percent.")
    _require(actor, "sales.apply_discount")
    limit = discount_limit(actor)
    if pct > limit:
        raise SaleError(f"Discounts above {limit}% are not allowed for your role. Ask the pharmacy manager.")
    if not (reason or "").strip():
        raise SaleError("Enter the reason for the discount.")
    return pct.quantize(Decimal("0.01"))


def _validate_payments(payments):
    allowed, cleaned = enabled_methods(), []
    for p in payments or []:
        amount = money(p["amount"])
        if amount <= 0:
            raise SaleError("A payment amount must be more than zero.")
        if p["method"] not in allowed:
            raise SaleError(f"{p['method']} payments are not enabled.")
        if p["method"] in (PaymentMethod.UPI, PaymentMethod.CARD) and not p.get("confirmed"):
            raise SaleError("Tick the box to confirm that the UPI / card payment has been received.")
        cleaned.append({"method": p["method"], "amount": amount, "reference": (p.get("reference") or "").strip()})
    if not cleaned:
        raise SaleError("Take at least one payment.")
    return cleaned


def complete_counter_sale(*, actor, request, sale_id, payments, discount_percent=0, discount_reason="",
                          customer_name="", customer_phone="", expected_subtotal=None, restricted=None, today=None):
    _require(actor, SELL)
    today = today or timezone.localdate()
    with transaction.atomic():
        sale = Sale.objects.select_for_update().filter(pk=sale_id, channel=COUNTER).first()
        if sale is None:
            raise SaleError("That bill was not found.")
        if sale.cashier_id != actor.pk:
            raise SaleError("This bill belongs to another cashier.")
        if sale.status == Sale.Status.COMPLETED:
            return sale  # a retry or a double click: already done, nothing is taken twice
        shift = current_shift(actor)
        if shift is None:
            raise SaleError("Open your shift before billing.")
        open_lines = list(sale.lines.select_related("medicine").order_by("id"))
        if not open_lines:
            raise SaleError("The bill is empty.")
        for line in open_lines:
            _check_sellable(line.medicine)
        approver = None
        if any(needs_approval(line.medicine.schedule_category) for line in open_lines):
            approver = _verify_restricted(request, restricted)
        pct = _validate_discount(actor, discount_percent, discount_reason)
        cleaned_payments = _validate_payments(payments)

        taken = []
        for line in open_lines:
            try:
                result = stock.consume_stock(
                    actor=actor, request=request, medicine_id=line.medicine_id, quantity=line.quantity,
                    reference_type="sale", reference_id=sale.pk, channel=COUNTER, permission=SELL, today=today)
            except stock.StockError as exc:
                raise SaleError(f"{line.medicine.brand_name}: {exc}")
            taken.append((line, result.allocations))

        final, subtotal, discount_total = [], ZERO, ZERO
        for line, allocations in taken:
            for a in allocations:
                gross = money(Decimal(a.quantity) * a.unit_price)
                disc = money(gross * pct / Decimal(100)) if pct else ZERO
                final.append(dict(medicine=line.medicine, batch=a.batch, description=line.description,
                                  batch_number=a.batch.batch_number, expiry_date=a.batch.expiry_date, quantity=a.quantity,
                                  unit_price=a.unit_price, unit_cost=a.unit_cost, line_total=gross, discount_amount=disc))
                subtotal += gross
                discount_total += disc
        total = subtotal - discount_total
        if expected_subtotal is not None and money(expected_subtotal) != subtotal:
            raise SaleError("Prices or stock changed while this bill was open. Check the bill and confirm again.")
        if total <= 0:
            raise SaleError("The bill total must be more than zero.")
        paid = sum((p["amount"] for p in cleaned_payments), ZERO)
        if paid != total:
            raise SaleError(f"The payments (\u20b9{paid}) must add up to the bill total (\u20b9{total}).")

        sale.lines.all().delete()
        for data in final:
            SaleLine.objects.create(sale=sale, **data)
        number = next_number(DocType.COUNTER_SALE, period=f"{today:%Y}")
        sale.number, sale.status, sale.shift = number.formatted, Sale.Status.COMPLETED, shift
        sale.customer_name, sale.customer_phone = (customer_name or "").strip(), (customer_phone or "").strip()
        sale.subtotal, sale.discount_percent, sale.discount_total = subtotal, pct, discount_total
        sale.discount_reason = (discount_reason or "").strip() if pct else ""
        sale.total, sale.amount_paid, sale.completed_at = total, paid, timezone.now()
        if approver is not None:
            sale.rx_prescriber_name = restricted["prescriber_name"].strip()
            sale.rx_prescriber_reg_no = restricted["prescriber_reg_no"].strip()
            sale.rx_patient_name = restricted["patient_name"].strip()
            sale.rx_approved_by, sale.rx_approved_at = approver, timezone.now()
        sale.save()
        for p in cleaned_payments:
            Payment.objects.create(sale=sale, channel=COUNTER, kind=Payment.Kind.PAYMENT, method=p["method"],
                                   amount=p["amount"], reference=p["reference"], shift=shift, received_by=actor)
        log_event(user=actor, action="sale.complete", obj=sale, summary=f"{sale.number} total {sale.total}",
                  request=request)
        if approver is not None:
            log_event(user=actor, action="sale.restricted_approved", obj=sale,
                      summary=f"Approved by {approver.get_username()}", request=request)
    return sale
