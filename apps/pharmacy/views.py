from datetime import date as date_cls, datetime, time, timedelta
from decimal import Decimal

from django.contrib import messages
from django.db import IntegrityError, transaction
from django.db.models import Q, Sum
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from apps.accounts.decorators import permission_required
from apps.accounts.permissions import user_has_permission
from apps.core.audit import log_event

from . import purchasing, stock
from .csvutil import csv_response
from .forms import AdjustForm, MedicineForm, PurchaseHeaderForm, PurchaseItemForm, QuantityReasonForm, ReasonForm, SupplierForm
from .models import (InventoryTransaction, Medicine, MedicineBatch, PurchaseInvoice, PurchaseInvoiceItem, Supplier)

VIEW, COSTS, ADJUST, PURCHASES = ("pharmacy.view_stock", "pharmacy.view_costs", "pharmacy.adjust_stock",
                                  "pharmacy.manage_purchases")


def _costs(request):
    return user_has_permission(request.user, COSTS)


def _day(request, name):
    try:
        return date_cls.fromisoformat(request.GET.get(name, ""))
    except ValueError:
        return None


# ---------------------------------------------------------------- catalogue
@permission_required(VIEW)
def medicine_list(request):
    q = request.GET.get("q", "").strip()
    meds = Medicine.objects.all()
    if q:
        meds = meds.filter(Q(brand_name__icontains=q) | Q(generic_name__icontains=q))
    can_manage = user_has_permission(request.user, "pharmacy.manage_catalogue")
    if request.GET.get("fmt") == "csv" and can_manage:
        return csv_response("medicine-locations.csv",
                            ["brand_name", "strength", "dosage_form", "item_code", "rack", "shelf", "drawer", "box", "storage_notes"],
                            [[m.brand_name, m.strength, m.dosage_form, m.item_code, m.rack, m.shelf, m.drawer, m.box, m.storage_notes]
                             for m in meds])
    return render(request, "pharmacy/medicine_list.html", {"medicines": meds[:300], "q": q, "can_manage": can_manage})


def _save_medicine(request, form, creating):
    try:
        with transaction.atomic():
            changed = list(form.changed_data)
            med = form.save(commit=False)
            if creating:
                med.created_by = request.user
            med.save()
            log_event(user=request.user, action="medicine.create" if creating else "medicine.update", obj=med,
                      summary="" if creating else "Fields changed: " + ", ".join(changed), request=request)
    except IntegrityError:
        form.add_error(None, "A medicine with the same brand, strength and form already exists.")
        return None
    return med


@permission_required("pharmacy.manage_catalogue")
def medicine_create(request):
    form = MedicineForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        if _save_medicine(request, form, True):
            messages.success(request, "Medicine added to the catalogue.")
            return redirect("pharmacy:list")
    return render(request, "pharmacy/medicine_form.html", {"form": form, "creating": True})


@permission_required("pharmacy.manage_catalogue")
def medicine_edit(request, pk):
    med = get_object_or_404(Medicine, pk=pk)
    form = MedicineForm(request.POST or None, instance=med)
    if request.method == "POST" and form.is_valid():
        if _save_medicine(request, form, False):
            messages.success(request, "Medicine saved.")
            return redirect("pharmacy:list")
    return render(request, "pharmacy/medicine_form.html", {"form": form, "creating": False, "medicine": med})


@permission_required(VIEW)
def medicine_detail(request, pk):
    med = get_object_or_404(Medicine, pk=pk)
    today = timezone.localdate()
    batches = list(med.batches.all())
    for b in batches:
        stock.annotate_status(b, today)
    return render(request, "pharmacy/medicine_detail.html", {
        "medicine": med, "batches": batches, "costs": _costs(request), "labels": stock.STATUS_LABELS,
        "ledger": InventoryTransaction.objects.filter(medicine=med).select_related("batch", "created_by")[:25],
        "can_manage": user_has_permission(request.user, "pharmacy.manage_catalogue")})


# ---------------------------------------------------------------- stock views
@permission_required(VIEW)
def stock_overview(request):
    show = request.GET.get("show", "")
    rows = stock.medicine_stock_rows(show=show)
    return render(request, "pharmacy/stock_overview.html", {"rows": rows, "show": show})


def _batch_rows(request, include_empty=False):
    today = timezone.localdate()
    qs = MedicineBatch.objects.select_related("medicine", "supplier").order_by("medicine__brand_name", "expiry_date", "id")
    if not include_empty:
        qs = qs.filter(quantity_on_hand__gt=0)
    status = request.GET.get("status", "")
    rows = []
    for b in qs:
        stock.annotate_status(b, today)
        if not status or b.status == status:
            rows.append(b)
    return rows


@permission_required(VIEW)
def batch_report(request):
    rows, costs = _batch_rows(request, include_empty=request.GET.get("all") == "1"), _costs(request)
    if request.GET.get("fmt") == "csv":
        header = ["Medicine", "Strength", "Form", "Batch", "Expiry", "Status", "Units on hand", "Supplier", "Received"]
        header += ["Cost per unit", "Selling price per unit", "Value at cost"] if costs else ["Selling price per unit"]
        body = []
        for b in rows:
            line = [b.medicine.brand_name, b.medicine.strength, b.medicine.dosage_form, b.batch_number,
                    b.expiry_date.isoformat(), stock.STATUS_LABELS[b.status], b.quantity_on_hand,
                    b.supplier.name if b.supplier else "", b.received_date.isoformat()]
            if costs:
                line += [b.purchase_price_per_unit, b.selling_price_per_unit,
                         (b.purchase_price_per_unit * b.quantity_on_hand).quantize(Decimal("0.01"))]
            else:
                line += [b.selling_price_per_unit]
            body.append(line)
        return csv_response("stock-by-batch.csv", header, body)
    return render(request, "pharmacy/batch_report.html", {
        "rows": rows, "costs": costs, "labels": stock.STATUS_LABELS, "status": request.GET.get("status", ""),
        "show_all": request.GET.get("all") == "1"})


@permission_required(VIEW)
def expiry_report(request):
    today = timezone.localdate()
    try:
        days = max(int(request.GET.get("days", stock.near_expiry_days())), 0)
    except ValueError:
        days = stock.near_expiry_days()
    limit = today + timedelta(days=days)
    batches = (MedicineBatch.objects.filter(quantity_on_hand__gt=0).filter(Q(expiry_date__lte=limit) | Q(is_recalled=True))
               .select_related("medicine", "supplier").order_by("expiry_date", "id"))
    rows = []
    for b in batches:
        stock.annotate_status(b, today)
        b.days_left = (b.expiry_date - today).days
        rows.append(b)
    if request.GET.get("fmt") == "csv":
        return csv_response("expiry-report.csv",
                            ["Medicine", "Strength", "Batch", "Expiry", "Days left", "Status", "Units on hand", "Supplier"],
                            [[b.medicine.brand_name, b.medicine.strength, b.batch_number, b.expiry_date.isoformat(),
                              b.days_left, stock.STATUS_LABELS[b.status], b.quantity_on_hand,
                              b.supplier.name if b.supplier else ""] for b in rows])
    return render(request, "pharmacy/expiry_report.html", {"rows": rows, "days": days, "labels": stock.STATUS_LABELS,
                                                           "costs": _costs(request)})


def _ledger_qs(request):
    qs = InventoryTransaction.objects.select_related("batch", "medicine", "created_by")
    kind, med = request.GET.get("kind", ""), request.GET.get("medicine", "")
    if kind in InventoryTransaction.Kind.values:
        qs = qs.filter(kind=kind)
    if med.isdigit():
        qs = qs.filter(medicine_id=int(med))
    start, end = _day(request, "from"), _day(request, "to")
    tz = timezone.get_current_timezone()
    if start:
        qs = qs.filter(created_at__gte=timezone.make_aware(datetime.combine(start, time.min), tz))
    if end:
        qs = qs.filter(created_at__lt=timezone.make_aware(datetime.combine(end + timedelta(days=1), time.min), tz))
    return qs


@permission_required(VIEW)
def ledger(request):
    qs, costs = _ledger_qs(request), _costs(request)
    if request.GET.get("fmt") == "csv":
        header = ["When", "Medicine", "Batch", "Type", "Change", "Balance after", "Reason", "By"] + (["Unit cost"] if costs else [])
        body = [[timezone.localtime(t.created_at).strftime("%d/%m/%Y %H:%M"), t.medicine.brand_name, t.batch.batch_number,
                 t.get_kind_display(), t.quantity_change, t.balance_after, t.reason,
                 t.created_by.username if t.created_by else ""] + ([t.unit_cost] if costs else []) for t in qs[:5000]]
        return csv_response("stock-ledger.csv", header, body)
    return render(request, "pharmacy/ledger.html", {
        "rows": qs[:300], "kinds": InventoryTransaction.Kind.choices, "medicines": Medicine.objects.all(),
        "f": {k: request.GET.get(k, "") for k in ("kind", "medicine", "from", "to")}, "costs": costs})


@permission_required(COSTS)
def valuation(request):
    totals, rows = stock.valuation(), stock.valuation_by_medicine()
    if request.GET.get("fmt") == "csv":
        return csv_response("stock-valuation.csv", ["Medicine", "Strength", "Form", "Units", "Value at cost", "Value at selling price"],
                            [[r["medicine__brand_name"], r["medicine__strength"], r["medicine__dosage_form"], r["units"],
                              r["cost"], r["sell"]] for r in rows])
    return render(request, "pharmacy/valuation.html", {"t": totals, "rows": rows})


# ---------------------------------------------------------------- batch detail + manual changes
@permission_required(VIEW)
def batch_detail(request, pk):
    batch = get_object_or_404(MedicineBatch.objects.select_related("medicine", "supplier"), pk=pk)
    stock.annotate_status(batch)
    return render(request, "pharmacy/batch_detail.html", {
        "batch": batch, "costs": _costs(request), "labels": stock.STATUS_LABELS,
        "ledger": batch.ledger.select_related("created_by")[:100],
        "can_adjust": user_has_permission(request.user, ADJUST),
        "can_return": user_has_permission(request.user, PURCHASES),
        "adjust_form": AdjustForm(auto_id="id_adj_%s"), "wastage_form": QuantityReasonForm(auto_id="id_waste_%s"),
        "return_form": QuantityReasonForm(auto_id="id_ret_%s"), "recall_form": ReasonForm(auto_id="id_recall_%s")})


def _batch_action(request, pk, form_class, call, success, permission):
    batch = get_object_or_404(MedicineBatch, pk=pk)
    form = form_class(request.POST)
    if form.is_valid():
        try:
            call(batch, form.cleaned_data)
        except stock.StockError as exc:
            messages.error(request, str(exc))
        else:
            messages.success(request, success)
    else:
        messages.error(request, "; ".join(e for errs in form.errors.values() for e in errs))
    return redirect("pharmacy:batch_detail", pk=pk)


@permission_required(ADJUST)
@require_POST
def batch_adjust(request, pk):
    def call(b, d):
        delta = d["quantity"] if d["direction"] == "add" else -d["quantity"]
        stock.adjust_stock(actor=request.user, request=request, batch_id=b.pk, delta=delta, reason=d["reason"])
    return _batch_action(request, pk, AdjustForm, call, "Stock adjusted.", ADJUST)


@permission_required(ADJUST)
@require_POST
def batch_wastage(request, pk):
    return _batch_action(request, pk, QuantityReasonForm, lambda b, d: stock.record_wastage(
        actor=request.user, request=request, batch_id=b.pk, quantity=d["quantity"], reason=d["reason"]),
        "Wastage recorded.", ADJUST)


@permission_required(PURCHASES)
@require_POST
def batch_return(request, pk):
    return _batch_action(request, pk, QuantityReasonForm, lambda b, d: stock.return_to_supplier(
        actor=request.user, request=request, batch_id=b.pk, quantity=d["quantity"], reason=d["reason"]),
        "Return to supplier recorded.", PURCHASES)


@permission_required(ADJUST)
@require_POST
def batch_recall(request, pk):
    return _batch_action(request, pk, ReasonForm, lambda b, d: stock.set_recall(
        actor=request.user, request=request, batch_id=b.pk, recalled=True, reason=d["reason"]),
        "Batch marked as recalled. It can no longer be dispensed.", ADJUST)


@permission_required(ADJUST)
@require_POST
def batch_unrecall(request, pk):
    batch = get_object_or_404(MedicineBatch, pk=pk)
    stock.set_recall(actor=request.user, request=request, batch_id=batch.pk, recalled=False)
    messages.success(request, "Recall cleared.")
    return redirect("pharmacy:batch_detail", pk=pk)


# ---------------------------------------------------------------- suppliers
@permission_required(PURCHASES)
def supplier_list(request):
    return render(request, "pharmacy/supplier_list.html", {"suppliers": Supplier.objects.all()})


def _supplier_form(request, instance=None):
    form = SupplierForm(request.POST or None, instance=instance)
    if request.method == "POST" and form.is_valid():
        with transaction.atomic():
            changed = list(form.changed_data)
            sup = form.save()
            log_event(user=request.user, action="supplier.update" if instance else "supplier.create", obj=sup,
                      summary=("Fields changed: " + ", ".join(changed)) if instance else "", request=request)
        messages.success(request, "Supplier saved.")
        return redirect("pharmacy:supplier_list")
    return render(request, "pharmacy/supplier_form.html", {"form": form, "supplier": instance})


@permission_required(PURCHASES)
def supplier_create(request):
    return _supplier_form(request)


@permission_required(PURCHASES)
def supplier_edit(request, pk):
    return _supplier_form(request, get_object_or_404(Supplier, pk=pk))


# ---------------------------------------------------------------- purchases
@permission_required(PURCHASES)
def purchase_list(request):
    return render(request, "pharmacy/purchase_list.html", {
        "purchases": PurchaseInvoice.objects.select_related("supplier")[:200], "costs": _costs(request)})


@permission_required(PURCHASES)
def purchase_create(request):
    form = PurchaseHeaderForm(request.POST or None, initial={"invoice_date": timezone.localdate()})
    if request.method == "POST" and form.is_valid():
        try:
            inv = purchasing.create_draft(actor=request.user, request=request, **form.cleaned_data)
        except purchasing.PurchaseError as exc:
            form.add_error(None, str(exc))
        else:
            return redirect("pharmacy:purchase_detail", pk=inv.pk)
    return render(request, "pharmacy/purchase_form.html", {"form": form, "creating": True})


@permission_required(PURCHASES)
def purchase_edit(request, pk):
    inv = get_object_or_404(PurchaseInvoice, pk=pk)
    if inv.status != PurchaseInvoice.Status.DRAFT:
        messages.error(request, "A received purchase cannot be edited.")
        return redirect("pharmacy:purchase_detail", pk=pk)
    form = PurchaseHeaderForm(request.POST or None, instance=inv)
    if request.method == "POST" and form.is_valid():
        try:
            purchasing.update_header(actor=request.user, request=request, invoice_id=pk, **form.cleaned_data)
        except purchasing.PurchaseError as exc:
            form.add_error(None, str(exc))
        else:
            messages.success(request, "Saved.")
            return redirect("pharmacy:purchase_detail", pk=pk)
    return render(request, "pharmacy/purchase_form.html", {"form": form, "creating": False, "invoice": inv})


def _purchase_page(request, inv, item_form=None):
    return render(request, "pharmacy/purchase_detail.html", {
        "inv": inv, "items": inv.items.select_related("medicine"), "item_form": item_form or PurchaseItemForm(),
        "draft": inv.status == PurchaseInvoice.Status.DRAFT})


@permission_required(PURCHASES)
def purchase_detail(request, pk):
    return _purchase_page(request, get_object_or_404(PurchaseInvoice.objects.select_related("supplier"), pk=pk))


@permission_required(PURCHASES)
@require_POST
def purchase_item_add(request, pk):
    inv = get_object_or_404(PurchaseInvoice.objects.select_related("supplier"), pk=pk)
    form = PurchaseItemForm(request.POST)
    if form.is_valid():
        try:
            purchasing.add_item(actor=request.user, request=request, invoice_id=inv.pk, **form.cleaned_data)
        except purchasing.PurchaseError as exc:
            messages.error(request, str(exc))
        else:
            messages.success(request, "Item added.")
            return redirect("pharmacy:purchase_detail", pk=pk)
    return _purchase_page(request, inv, item_form=form)


@permission_required(PURCHASES)
@require_POST
def purchase_item_remove(request, pk, item_pk):
    item = get_object_or_404(PurchaseInvoiceItem, pk=item_pk, invoice_id=pk)
    try:
        purchasing.remove_item(actor=request.user, request=request, item_id=item.pk)
        messages.success(request, "Item removed.")
    except purchasing.PurchaseError as exc:
        messages.error(request, str(exc))
    return redirect("pharmacy:purchase_detail", pk=pk)


@permission_required(PURCHASES)
@require_POST
def purchase_receive(request, pk):
    inv = get_object_or_404(PurchaseInvoice, pk=pk)
    try:
        inv = purchasing.receive(actor=request.user, request=request, invoice_id=inv.pk)
    except purchasing.PurchaseError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, f"Received into stock as {inv.number}.")
    return redirect("pharmacy:purchase_detail", pk=pk)


@permission_required(PURCHASES)
@require_POST
def purchase_discard(request, pk):
    inv = get_object_or_404(PurchaseInvoice, pk=pk)
    try:
        purchasing.discard_draft(actor=request.user, request=request, invoice_id=inv.pk)
    except purchasing.PurchaseError as exc:
        messages.error(request, str(exc))
        return redirect("pharmacy:purchase_detail", pk=pk)
    messages.success(request, "Draft discarded.")
    return redirect("pharmacy:purchase_list")


@permission_required(COSTS)
def purchase_report(request):
    start, end = _day(request, "from"), _day(request, "to")
    qs = PurchaseInvoice.objects.filter(status=PurchaseInvoice.Status.RECEIVED).select_related("supplier")
    if start:
        qs = qs.filter(invoice_date__gte=start)
    if end:
        qs = qs.filter(invoice_date__lte=end)
    by_supplier = qs.values("supplier__name").annotate(total=Sum("grand_total"), tax=Sum("tax_amount"),
                                                       discount=Sum("discount_amount")).order_by("supplier__name")
    grand = qs.aggregate(total=Sum("grand_total"))["total"] or 0
    if request.GET.get("fmt") == "csv":
        return csv_response("purchases.csv", ["Entry no.", "Supplier invoice", "Invoice date", "Supplier", "Subtotal",
                                              "Discount", "Tax", "Total"],
                            [[i.number, i.supplier_invoice_number, i.invoice_date.isoformat(), i.supplier.name, i.subtotal,
                              i.discount_amount, i.tax_amount, i.grand_total] for i in qs])
    return render(request, "pharmacy/purchase_report.html", {
        "invoices": qs, "by_supplier": by_supplier, "grand": grand, "f": {"from": request.GET.get("from", ""), "to": request.GET.get("to", "")}})
