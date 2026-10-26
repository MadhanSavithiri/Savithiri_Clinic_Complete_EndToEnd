"""What the counter salesperson may see. Everything here reads an explicit list of fields: purchase prices,
suppliers and margins are never loaded, so they cannot leak into a page, a receipt or a download."""
from datetime import timedelta
from decimal import Decimal

from django.db.models import Q
from django.utils import timezone

from apps.core.models import Setting
from apps.patients.normalize import normalize_name
from apps.pharmacy import stock
from apps.pharmacy.models import Medicine, MedicineBatch

from .models import ZERO
from .rules import BLOCKED_CATEGORIES, needs_approval

PUBLIC_BATCH_FIELDS = ("batch_number", "expiry_date", "quantity_on_hand", "selling_price_per_unit")


def public_batches(medicine_id, today=None):
    """Usable batches in FEFO order, with only the fields a salesperson may see."""
    today = today or timezone.localdate()
    return list(MedicineBatch.objects.filter(medicine_id=medicine_id).filter(stock.usable_filter(today))
                .order_by("expiry_date", "received_date", "id").values(*PUBLIC_BATCH_FIELDS))


def money(value):
    return Decimal(value).quantize(Decimal("0.01"))


def preview(medicine_id, quantity, today=None):
    """Which batches would be used for `quantity`, FEFO, and what it would cost. No locks, no changes."""
    remaining, parts = quantity, []
    for b in public_batches(medicine_id, today):
        take = min(b["quantity_on_hand"], remaining)
        if take:
            parts.append({"batch_number": b["batch_number"], "expiry_date": b["expiry_date"], "quantity": take,
                          "unit_price": b["selling_price_per_unit"],
                          "line_total": money(Decimal(take) * b["selling_price_per_unit"])})
            remaining -= take
        if remaining == 0:
            break
    return parts, remaining


def search_items(query, limit=30, today=None):
    """Items the counter may sell, matched by name, brand, generic name, strength, item code or location."""
    today = today or timezone.localdate()
    q = (query or "").strip()
    meds = Medicine.objects.filter(is_active=True, counter_sale_allowed=True)
    if q:
        cond = Q(item_code__iexact=q) | Q(rack__iexact=q) | Q(shelf__iexact=q) | Q(box__iexact=q) | Q(drawer__iexact=q)
        words = normalize_name(q).split()
        name_cond = Q()
        for w in words:
            name_cond &= (Q(brand_name__icontains=w) | Q(generic_name__icontains=w) | Q(strength__icontains=w))
        if words:
            cond |= name_cond
        meds = meds.filter(cond)
    near_until = today + timedelta(days=stock.near_expiry_days())
    results = []
    for m in meds.order_by("brand_name", "strength")[:limit]:
        batches = public_batches(m.pk, today)
        usable = sum(b["quantity_on_hand"] for b in batches)
        first = batches[0] if batches else None
        prices = {b["selling_price_per_unit"] for b in batches}
        results.append({
            "id": m.pk, "name": m.brand_name, "strength": m.strength, "form": m.dosage_form, "generic": m.generic_name,
            "unit": m.unit, "item_code": m.item_code, "location": m.location_label, "storage_notes": m.storage_notes,
            "usable": usable, "price": first["selling_price_per_unit"] if first else None, "price_varies": len(prices) > 1,
            "batch_number": first["batch_number"] if first else "", "expiry": first["expiry_date"] if first else None,
            "near_expiry": bool(first and first["expiry_date"] <= near_until),
            "needs_approval": needs_approval(m.schedule_category),
            "blocked": m.schedule_category in BLOCKED_CATEGORIES and not Setting.get("counter_allow_schedule_x", False),
            "category": m.get_schedule_category_display(),
        })
    return results


def cart_context(draft, today=None):
    """The open bill as the salesperson sees it, priced from the batches that would be used right now."""
    rows, subtotal, problems, restricted = [], ZERO, False, False
    if draft is not None:
        for line in draft.lines.select_related("medicine"):
            parts, short = preview(line.medicine_id, line.quantity, today)
            total = sum((p["line_total"] for p in parts), ZERO)
            needs = needs_approval(line.medicine.schedule_category)
            rows.append({"line": line, "medicine": line.medicine, "location": line.medicine.location_label, "parts": parts,
                         "total": total, "short": short, "needs_approval": needs})
            subtotal += total
            problems = problems or bool(short)
            restricted = restricted or needs
    return {"rows": rows, "subtotal": subtotal, "has_problem": problems, "has_restricted": restricted}
