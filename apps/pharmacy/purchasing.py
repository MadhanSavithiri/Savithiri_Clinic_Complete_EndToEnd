"""Purchase entry: supplier invoice -> draft lines -> 'receive' (creates batches + ledger rows, once)."""
from decimal import Decimal

from django.core.exceptions import PermissionDenied
from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.accounts.permissions import user_has_permission
from apps.core.audit import log_event
from apps.core.models import DocType
from apps.core.sequences import next_number

from .models import (InventoryTransaction, MedicineBatch, PurchaseInvoice, PurchaseInvoiceItem)

Status = PurchaseInvoice.Status
PERM = "pharmacy.manage_purchases"


class PurchaseError(Exception):
    """A purchasing rule was broken; the message is safe to show to staff."""


def _require(actor):
    if not user_has_permission(actor, PERM):
        raise PermissionDenied


def create_draft(*, actor, request, supplier, supplier_invoice_number, invoice_date, discount_amount=Decimal("0"),
                 tax_amount=Decimal("0"), notes=""):
    _require(actor)
    if not supplier.is_active:
        raise PurchaseError("This supplier is inactive.")
    try:
        with transaction.atomic():
            inv = PurchaseInvoice.objects.create(
                supplier=supplier, supplier_invoice_number=supplier_invoice_number.strip(), invoice_date=invoice_date,
                discount_amount=discount_amount, tax_amount=tax_amount, notes=notes, created_by=actor)
            log_event(user=actor, action="purchase.create_draft", obj=inv, request=request)
    except IntegrityError:
        raise PurchaseError("This supplier invoice number has already been entered.")
    return inv


def update_header(*, actor, request, invoice_id, **fields):
    _require(actor)
    with transaction.atomic():
        inv = PurchaseInvoice.objects.select_for_update().get(pk=invoice_id)
        if inv.status != Status.DRAFT:
            raise PurchaseError("This purchase has been received and can no longer be edited.")
        for k, v in fields.items():
            setattr(inv, k, v)
        try:
            with transaction.atomic():
                inv.save()
        except IntegrityError:
            raise PurchaseError("This supplier invoice number has already been entered.")
        log_event(user=actor, action="purchase.update_header", obj=inv, request=request)
    return inv


def add_item(*, actor, request, invoice_id, medicine, batch_number, expiry_date, packs, units_per_pack,
             purchase_price_per_pack, mrp_per_pack, today=None):
    _require(actor)
    today = today or timezone.localdate()
    if expiry_date <= today:
        raise PurchaseError("This batch is already expired. Expired goods cannot be received into stock.")
    if not medicine.is_active:
        raise PurchaseError("This medicine is inactive in the catalogue.")
    with transaction.atomic():
        inv = PurchaseInvoice.objects.select_for_update().get(pk=invoice_id)
        if inv.status != Status.DRAFT:
            raise PurchaseError("This purchase has been received and can no longer be edited.")
        item = PurchaseInvoiceItem.objects.create(
            invoice=inv, medicine=medicine, batch_number=batch_number.strip(), expiry_date=expiry_date, packs=packs,
            units_per_pack=units_per_pack, purchase_price_per_pack=purchase_price_per_pack, mrp_per_pack=mrp_per_pack)
        log_event(user=actor, action="purchase.add_item", obj=inv, request=request)
    return item


def remove_item(*, actor, request, item_id):
    _require(actor)
    with transaction.atomic():
        item = PurchaseInvoiceItem.objects.select_related("invoice").get(pk=item_id)
        inv = PurchaseInvoice.objects.select_for_update().get(pk=item.invoice_id)
        if inv.status != Status.DRAFT:
            raise PurchaseError("This purchase has been received and can no longer be edited.")
        item.delete()
        log_event(user=actor, action="purchase.remove_item", obj=inv, request=request)


def discard_draft(*, actor, request, invoice_id):
    _require(actor)
    with transaction.atomic():
        inv = PurchaseInvoice.objects.select_for_update().get(pk=invoice_id)
        if inv.status != Status.DRAFT:
            raise PurchaseError("Only a draft can be discarded.")
        pk = inv.pk
        inv.delete()
        log_event(user=actor, action="purchase.discard_draft", summary=f"Draft {pk} discarded", request=request)


def receive(*, actor, request, invoice_id, today=None):
    """Put the goods into stock. Happens exactly once: a second click or a second PC is refused."""
    _require(actor)
    today = today or timezone.localdate()
    with transaction.atomic():
        inv = PurchaseInvoice.objects.select_for_update(of=("self",)).select_related("supplier").get(pk=invoice_id)
        if inv.status != Status.DRAFT:
            raise PurchaseError("This purchase has already been received.")
        items = list(inv.items.select_related("medicine"))
        if not items:
            raise PurchaseError("Add at least one item first.")
        subtotal = Decimal("0.00")
        for item in items:
            if item.expiry_date <= today:
                raise PurchaseError(f"{item.medicine.brand_name} batch {item.batch_number} is expired. Remove it first.")
            per_unit_cost = (item.purchase_price_per_pack / item.units_per_pack).quantize(Decimal("0.0001"))
            per_unit_sell = (item.mrp_per_pack / item.units_per_pack).quantize(Decimal("0.0001"))
            batch = MedicineBatch.objects.create(
                medicine=item.medicine, supplier=inv.supplier, purchase_item=item, batch_number=item.batch_number,
                expiry_date=item.expiry_date, received_date=today, quantity_received=item.quantity_units,
                quantity_on_hand=item.quantity_units, purchase_price_per_unit=per_unit_cost,
                selling_price_per_unit=per_unit_sell, created_by=actor)
            InventoryTransaction.objects.create(
                batch=batch, medicine=item.medicine, kind=InventoryTransaction.Kind.RECEIPT,
                quantity_change=item.quantity_units, balance_after=item.quantity_units, unit_cost=per_unit_cost,
                reference_type="purchase_invoice", reference_id=str(inv.pk), created_by=actor)
            subtotal += item.line_total
        number = next_number(DocType.PURCHASE_INVOICE, period=f"{today:%Y}")
        inv.number, inv.status = number.formatted, Status.RECEIVED
        inv.subtotal = subtotal
        inv.grand_total = subtotal - inv.discount_amount + inv.tax_amount
        inv.received_at, inv.received_by = timezone.now(), actor
        inv.save()
        log_event(user=actor, action="purchase.receive", obj=inv, summary=f"{inv.number}: {len(items)} line(s)", request=request)
    return inv
