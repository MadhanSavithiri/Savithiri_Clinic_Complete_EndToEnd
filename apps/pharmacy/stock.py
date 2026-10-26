"""Stock rules. All quantities are whole counting units (tablets, mL...). Every change is atomic, writes an
append-only ledger row, and locks batch rows (SELECT ... FOR UPDATE) so two workstations can never
over-sell the same stock."""
from collections import namedtuple
from datetime import timedelta
from decimal import Decimal

from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.db.models import DecimalField, ExpressionWrapper, F, IntegerField, Q, Sum, Value
from django.db.models.functions import Coalesce
from django.utils import timezone

from apps.accounts.permissions import user_has_permission
from apps.core.audit import log_event
from apps.core.models import Setting

from .models import InventoryTransaction, Medicine, MedicineBatch

Kind = InventoryTransaction.Kind
Allocation = namedtuple("Allocation", "batch quantity unit_price unit_cost")
StockResult = namedtuple("StockResult", "allocations shortfall")


class StockError(Exception):
    """A stock rule was broken; the message is safe to show to staff."""


def _require(actor, code):
    if not user_has_permission(actor, code):
        raise PermissionDenied


def _int_setting(key, default):
    try:
        return max(int(Setting.get(key, default)), 0)
    except (TypeError, ValueError):
        return default


def near_expiry_days():
    return _int_setting("near_expiry_days", 90)


def min_days_before_expiry():
    """Batches closer to expiry than this are not dispensed (0 = usable until the expiry date itself)."""
    return _int_setting("dispense_min_days_before_expiry", 0)


def batch_status(batch, today=None):
    today = today or timezone.localdate()
    if batch.is_recalled:
        return "RECALLED"
    if batch.expiry_date < today:
        return "EXPIRED"
    if batch.expiry_date <= today + timedelta(days=near_expiry_days()):
        return "NEAR_EXPIRY"
    return "OK"


STATUS_LABELS = {"RECALLED": "Recalled", "EXPIRED": "Expired", "NEAR_EXPIRY": "Near expiry", "OK": "OK"}


def annotate_status(batch, today=None):
    batch.status = batch_status(batch, today)
    batch.status_label = STATUS_LABELS[batch.status]
    return batch


def _usable_q(today):
    cutoff = today + timedelta(days=min_days_before_expiry())
    return Q(is_recalled=False, expiry_date__gte=cutoff, quantity_on_hand__gt=0)


def usable_filter(today):
    """Q filter for batches that may be sold today (not recalled, not expired, in stock)."""
    return _usable_q(today)


# ---------------------------------------------------------------- FEFO
def _allocate(medicine_id, quantity, today):
    """FEFO = First Expiry, First Out. Expired and recalled batches are never offered."""
    batches = (MedicineBatch.objects.select_for_update()
               .filter(medicine_id=medicine_id).filter(_usable_q(today))
               .order_by("expiry_date", "received_date", "id"))
    remaining, allocations = quantity, []
    for batch in batches:
        take = min(batch.quantity_on_hand, remaining)
        allocations.append((batch, take))
        remaining -= take
        if remaining == 0:
            break
    return allocations, remaining


def preview_fefo(medicine_id, quantity, today=None):
    """Read-only look at which batches would be used (no locks, no changes)."""
    today = today or timezone.localdate()
    batches = (MedicineBatch.objects.filter(medicine_id=medicine_id).filter(_usable_q(today))
               .order_by("expiry_date", "received_date", "id"))
    remaining, out = quantity, []
    for b in batches:
        take = min(b.quantity_on_hand, remaining)
        out.append((b, take))
        remaining -= take
        if remaining == 0:
            break
    return out, remaining


def _ledger(batch, kind, change, actor, reason="", ref_type="", ref_id="", channel=""):
    return InventoryTransaction.objects.create(
        batch=batch, medicine_id=batch.medicine_id, kind=kind, quantity_change=change,
        balance_after=batch.quantity_on_hand, unit_cost=batch.purchase_price_per_unit, reason=reason,
        reference_type=ref_type, reference_id=str(ref_id or ""), channel=channel, created_by=actor)


def consume_stock(*, actor, request, medicine_id, quantity, reference_type="", reference_id="",
                  allow_partial=False, today=None, channel="", permission="pharmacy.dispense"):
    """Take `quantity` units for a sale, FEFO, from the ONE shared stock. `channel` records which sales
    channel took it (clinic pharmacy or independent counter); `permission` is what the caller must hold.
    allow_partial=True returns what is available and the shortfall instead of failing."""
    _require(actor, permission)
    if int(quantity) < 1:
        raise StockError("Quantity must be at least 1.")
    today = today or timezone.localdate()
    with transaction.atomic():
        allocations, shortfall = _allocate(medicine_id, int(quantity), today)
        if shortfall and not allow_partial:
            raise StockError(f"Only {quantity - shortfall} unit(s) are available (not expired or recalled); "
                             f"{quantity} requested.")
        result = []
        for batch, take in allocations:
            batch.quantity_on_hand -= take
            batch.save(update_fields=["quantity_on_hand", "updated_at"])
            _ledger(batch, Kind.DISPENSE, -take, actor, ref_type=reference_type, ref_id=reference_id, channel=channel)
            result.append(Allocation(batch, take, batch.selling_price_per_unit, batch.purchase_price_per_unit))
    return StockResult(result, shortfall)


def available_units(medicine_id, today=None):
    """Usable units (not expired, not recalled) right now. A read-only look: use consume_stock to take stock."""
    today = today or timezone.localdate()
    total = MedicineBatch.objects.filter(medicine_id=medicine_id).filter(_usable_q(today)).aggregate(n=Sum("quantity_on_hand"))["n"]
    return total or 0


# ---------------------------------------------------------------- manual changes
def _apply(actor, request, batch_id, delta, kind, reason, permission, action):
    _require(actor, permission)
    reason = (reason or "").strip()
    if not reason:
        raise StockError("Enter a reason.")
    if delta == 0:
        raise StockError("The change cannot be zero.")
    with transaction.atomic():
        batch = MedicineBatch.objects.select_for_update().get(pk=batch_id)
        if batch.quantity_on_hand + delta < 0:
            raise StockError(f"Only {batch.quantity_on_hand} unit(s) are in this batch.")
        batch.quantity_on_hand += delta
        batch.save(update_fields=["quantity_on_hand", "updated_at"])
        _ledger(batch, kind, delta, actor, reason=reason)
        log_event(user=actor, action=action, obj=batch, summary=f"{kind} {delta:+d}", request=request)
    return batch


def adjust_stock(*, actor, request, batch_id, delta, reason):
    """Correct a count after a stock-take. Signed: positive adds, negative removes."""
    return _apply(actor, request, batch_id, int(delta), Kind.ADJUSTMENT, reason, "pharmacy.adjust_stock", "stock.adjust")


def record_wastage(*, actor, request, batch_id, quantity, reason):
    """Expired, damaged or spilled stock written off."""
    return _apply(actor, request, batch_id, -int(quantity), Kind.WASTAGE, reason, "pharmacy.adjust_stock", "stock.wastage")


def return_to_supplier(*, actor, request, batch_id, quantity, reason):
    return _apply(actor, request, batch_id, -int(quantity), Kind.SUPPLIER_RETURN, reason, "pharmacy.manage_purchases",
                  "stock.supplier_return")


def set_recall(*, actor, request, batch_id, recalled, reason=""):
    _require(actor, "pharmacy.adjust_stock")
    reason = (reason or "").strip()
    if recalled and not reason:
        raise StockError("Enter the reason for the recall.")
    with transaction.atomic():
        batch = MedicineBatch.objects.select_for_update().get(pk=batch_id)
        if batch.is_recalled == recalled:
            return batch
        batch.is_recalled = recalled
        batch.recall_reason = reason if recalled else ""
        batch.recalled_at = timezone.now() if recalled else None
        batch.recalled_by = actor if recalled else None
        batch.save(update_fields=["is_recalled", "recall_reason", "recalled_at", "recalled_by", "updated_at"])
        log_event(user=actor, action="stock.recall" if recalled else "stock.recall_cleared", obj=batch, request=request)
    return batch


# ---------------------------------------------------------------- reading stock
def _sum(field="batches__quantity_on_hand", **kw):
    return Coalesce(Sum(field, **kw), Value(0), output_field=IntegerField())


def medicine_stock_rows(today=None, show=None):
    """One row per active medicine with usable / expired / near-expiry quantities and warning flags."""
    today = today or timezone.localdate()
    cutoff = today + timedelta(days=min_days_before_expiry())
    near_until = today + timedelta(days=near_expiry_days())
    usable_q = Q(batches__is_recalled=False, batches__expiry_date__gte=cutoff)
    qs = Medicine.objects.filter(is_active=True).annotate(
        total=_sum(),
        usable=_sum(filter=usable_q),
        expired=_sum(filter=Q(batches__expiry_date__lt=today)),
        near=_sum(filter=usable_q & Q(batches__expiry_date__lte=near_until)),
        recalled=_sum(filter=Q(batches__is_recalled=True)),
    ).order_by("brand_name", "strength")
    rows = []
    for m in qs:
        rows.append({
            "medicine": m, "total": m.total, "usable": m.usable, "expired": m.expired, "near": m.near,
            "recalled": m.recalled,
            "out": m.usable == 0,
            "low": m.reorder_threshold > 0 and 0 < m.usable <= m.reorder_threshold,
        })
    tests = {
        "low": lambda r: r["low"] or (r["out"] and r["medicine"].reorder_threshold > 0),
        "out": lambda r: r["out"],
        "near": lambda r: r["near"] > 0,
        "expired": lambda r: r["expired"] > 0,
    }
    return [r for r in rows if tests[show](r)] if show in tests else rows


def _value(field):
    return ExpressionWrapper(F("quantity_on_hand") * F(field), output_field=DecimalField(max_digits=20, decimal_places=4))


def _money(value):
    return (value or Decimal("0")).quantize(Decimal("0.01"))


def valuation(today=None):
    """Stock value at cost and at selling price, split into usable / expired / recalled."""
    today = today or timezone.localdate()
    cost, sell = _value("purchase_price_per_unit"), _value("selling_price_per_unit")
    expired = Q(expiry_date__lt=today, is_recalled=False)
    recalled = Q(is_recalled=True)
    usable = Q(is_recalled=False, expiry_date__gte=today)
    agg = MedicineBatch.objects.filter(quantity_on_hand__gt=0).aggregate(
        cost_all=Sum(cost), sell_all=Sum(sell),
        cost_usable=Sum(cost, filter=usable), sell_usable=Sum(sell, filter=usable),
        cost_expired=Sum(cost, filter=expired), sell_expired=Sum(sell, filter=expired),
        cost_recalled=Sum(cost, filter=recalled), sell_recalled=Sum(sell, filter=recalled))
    return {k: _money(v) for k, v in agg.items()}


def valuation_by_medicine(today=None):
    cost, sell = _value("purchase_price_per_unit"), _value("selling_price_per_unit")
    rows = (MedicineBatch.objects.filter(quantity_on_hand__gt=0).values("medicine_id", "medicine__brand_name",
                                                                         "medicine__strength", "medicine__dosage_form")
            .annotate(units=Sum("quantity_on_hand"), cost=Sum(cost), sell=Sum(sell))
            .order_by("medicine__brand_name", "medicine__strength"))
    return [{**r, "cost": _money(r["cost"]), "sell": _money(r["sell"])} for r in rows]


def reconcile():
    """Batches whose quantity does not equal the sum of their ledger rows. Must always be empty."""
    return list(MedicineBatch.objects.annotate(ledger_sum=Coalesce(Sum("ledger__quantity_change"), Value(0)))
                .exclude(quantity_on_hand=F("ledger_sum")).values("id", "batch_number", "quantity_on_hand", "ledger_sum"))
