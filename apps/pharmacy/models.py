from decimal import Decimal

from django.conf import settings
from django.core.validators import MinValueValidator
from django.db import models
from django.utils import timezone

from apps.core.models import ImmutableModel, SalesChannel, TimeStampedModel

User = settings.AUTH_USER_MODEL
MONEY = {"max_digits": 12, "decimal_places": 2}
UNIT_MONEY = {"max_digits": 12, "decimal_places": 4}  # per-unit prices keep 4 decimals; bills round to 2


class DosageForm(models.TextChoices):
    TABLET = "Tablet", "Tablet"
    CAPSULE = "Capsule", "Capsule"
    SYRUP = "Syrup", "Syrup"
    SUSPENSION = "Suspension", "Suspension"
    INJECTION = "Injection", "Injection"
    DROPS = "Drops", "Drops"
    OINTMENT = "Ointment", "Ointment"
    CREAM = "Cream", "Cream"
    GEL = "Gel", "Gel"
    INHALER = "Inhaler", "Inhaler"
    POWDER = "Powder", "Powder / sachet"
    OTHER = "Other", "Other"


class ScheduleCategory(models.TextChoices):
    NONE = "NONE", "None recorded"
    H = "H", "Schedule H"
    H1 = "H1", "Schedule H1"
    X = "X", "Schedule X"
    OTHER = "OTHER", "Other restricted (ask the pharmacist)"


class ItemKind(models.TextChoices):
    MEDICINE = "MEDICINE", "Medicine"
    GENERAL = "GENERAL", "General healthcare item"


class Supplier(TimeStampedModel):
    name = models.CharField(max_length=200, unique=True)
    contact_person = models.CharField(max_length=150, blank=True)
    phone = models.CharField(max_length=30, blank=True)
    address = models.TextField(blank=True)
    gstin = models.CharField("GSTIN (if applicable)", max_length=20, blank=True)
    drug_licence_number = models.CharField("Supplier's drug licence no.", max_length=60, blank=True)
    notes = models.CharField(max_length=255, blank=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class Medicine(TimeStampedModel):
    """Clinic medicine catalogue. Starts EMPTY: nothing is pre-loaded or invented."""

    brand_name = models.CharField(max_length=150)
    generic_name = models.CharField(max_length=200, blank=True)
    strength = models.CharField(max_length=60, blank=True, help_text="e.g. 500 mg, 5 mg/5 mL")
    dosage_form = models.CharField(max_length=20, choices=DosageForm.choices, default=DosageForm.TABLET)
    manufacturer = models.CharField(max_length=150, blank=True)
    schedule_category = models.CharField(max_length=10, choices=ScheduleCategory.choices, default=ScheduleCategory.NONE)
    unit = models.CharField("Counting unit", max_length=20, default="tablet",
                            help_text="What you count and sell by: tablet, capsule, mL, vial...")
    pack_size = models.PositiveIntegerField(default=1, validators=[MinValueValidator(1)],
                                            help_text="Units in one pack (e.g. 10 tablets in a strip)")
    reorder_threshold = models.PositiveIntegerField(default=0, help_text="Warn when usable stock falls to this many units. 0 = no warning.")
    item_kind = models.CharField(max_length=10, choices=ItemKind.choices, default=ItemKind.MEDICINE)
    item_code = models.CharField("Item code / barcode", max_length=40, blank=True)
    # Where it sits on the shelves. One location per item, shared by all its batches.
    rack = models.CharField(max_length=20, blank=True)
    shelf = models.CharField(max_length=20, blank=True)
    drawer = models.CharField("Drawer / compartment", max_length=20, blank=True)
    box = models.CharField(max_length=20, blank=True)
    storage_notes = models.CharField(max_length=255, blank=True)
    counter_sale_allowed = models.BooleanField("May be sold at the independent counter", default=True)
    default_supplier = models.ForeignKey(Supplier, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    notes = models.CharField(max_length=255, blank=True)
    is_active = models.BooleanField(default=True)
    created_by = models.ForeignKey(User, null=True, on_delete=models.PROTECT, related_name="+")

    class Meta:
        ordering = ["brand_name", "strength"]
        constraints = [
            models.UniqueConstraint(fields=["brand_name", "strength", "dosage_form"], name="uniq_medicine_brand_strength_form"),
            models.CheckConstraint(condition=models.Q(pack_size__gte=1), name="medicine_pack_size_positive"),
            models.UniqueConstraint(fields=["item_code"], condition=~models.Q(item_code=""), name="uniq_item_code_when_set"),
        ]

    def __str__(self):
        label = " ".join(p for p in [self.brand_name, self.strength] if p)
        return f"{label} ({self.dosage_form})"

    @property
    def location_label(self):
        """e.g. 'Rack R02 → Shelf S03 → Box B12' (blank parts are left out)."""
        parts = [("Rack", self.rack), ("Shelf", self.shelf), ("Drawer", self.drawer), ("Box", self.box)]
        return " \u2192 ".join(f"{name} {value}" for name, value in parts if value)

    @property
    def label_with_generic(self):
        return f"{self} - {self.generic_name}" if self.generic_name else str(self)


# ---------------------------------------------------------------- purchasing
class PurchaseLockedError(Exception):
    """A received purchase cannot be edited. Fix stock with an adjustment or a return instead."""


class PurchaseInvoice(TimeStampedModel):
    class Status(models.TextChoices):
        DRAFT = "DRAFT", "Draft (not in stock yet)"
        RECEIVED = "RECEIVED", "Received into stock"

    supplier = models.ForeignKey(Supplier, on_delete=models.PROTECT, related_name="purchases")
    supplier_invoice_number = models.CharField(max_length=60)
    invoice_date = models.DateField()
    number = models.CharField("Our entry number", max_length=40, null=True, blank=True, unique=True)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.DRAFT)
    discount_amount = models.DecimalField(default=Decimal("0.00"), validators=[MinValueValidator(Decimal("0"))], **MONEY)
    tax_amount = models.DecimalField("Tax amount (as on the invoice)", default=Decimal("0.00"),
                                     validators=[MinValueValidator(Decimal("0"))], **MONEY)
    notes = models.CharField(max_length=255, blank=True)
    subtotal = models.DecimalField(default=Decimal("0.00"), **MONEY)      # fixed at receipt
    grand_total = models.DecimalField(default=Decimal("0.00"), **MONEY)   # fixed at receipt
    received_at = models.DateTimeField(null=True, blank=True)
    received_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    created_by = models.ForeignKey(User, null=True, on_delete=models.PROTECT, related_name="+")

    class Meta:
        ordering = ["-invoice_date", "-id"]
        constraints = [
            models.UniqueConstraint(fields=["supplier", "supplier_invoice_number"], name="uniq_supplier_invoice_number"),
            models.CheckConstraint(condition=models.Q(status="DRAFT") | models.Q(number__isnull=False),
                                   name="received_purchase_has_number"),
            models.CheckConstraint(condition=models.Q(discount_amount__gte=0, tax_amount__gte=0),
                                   name="purchase_discount_tax_non_negative"),
        ]

    @classmethod
    def from_db(cls, db, field_names, values):
        instance = super().from_db(db, field_names, values)
        instance._loaded_status = instance.__dict__.get("status")
        return instance

    def _is_locked(self):
        loaded = getattr(self, "_loaded_status", None)
        return (not self._state.adding) and loaded not in (None, self.Status.DRAFT)

    def save(self, *args, **kwargs):
        if self._is_locked():
            raise PurchaseLockedError("This purchase has been received into stock and can no longer be edited.")
        super().save(*args, **kwargs)
        self._loaded_status = self.status

    def delete(self, *args, **kwargs):
        if self._is_locked() or self.status != self.Status.DRAFT:
            raise PurchaseLockedError("Only a draft purchase can be discarded.")
        return super().delete(*args, **kwargs)

    @property
    def lines_total(self):
        if self.status == self.Status.RECEIVED:
            return self.subtotal
        return sum((i.line_total for i in self.items.all()), Decimal("0.00"))

    @property
    def total_payable(self):
        if self.status == self.Status.RECEIVED:
            return self.grand_total
        return self.lines_total - self.discount_amount + self.tax_amount

    def __str__(self):
        return self.number or f"Draft {self.pk}"


class PurchaseInvoiceItem(TimeStampedModel):
    invoice = models.ForeignKey(PurchaseInvoice, on_delete=models.CASCADE, related_name="items")
    medicine = models.ForeignKey(Medicine, on_delete=models.PROTECT, related_name="+")
    batch_number = models.CharField(max_length=40)
    expiry_date = models.DateField()
    packs = models.PositiveIntegerField(validators=[MinValueValidator(1)])
    units_per_pack = models.PositiveIntegerField(validators=[MinValueValidator(1)])
    purchase_price_per_pack = models.DecimalField(validators=[MinValueValidator(Decimal("0"))], **MONEY)
    mrp_per_pack = models.DecimalField("Selling price (MRP) per pack", validators=[MinValueValidator(Decimal("0"))], **MONEY)
    quantity_units = models.PositiveIntegerField(editable=False, default=0)
    line_total = models.DecimalField(editable=False, default=Decimal("0.00"), **MONEY)

    class Meta:
        ordering = ["id"]
        constraints = [
            models.CheckConstraint(condition=models.Q(packs__gte=1, units_per_pack__gte=1), name="purchase_item_qty_positive"),
            models.CheckConstraint(condition=models.Q(purchase_price_per_pack__gte=0, mrp_per_pack__gte=0),
                                   name="purchase_item_prices_non_negative"),
        ]

    def _check_draft(self):
        if self.invoice.status != PurchaseInvoice.Status.DRAFT:
            raise PurchaseLockedError("This purchase has been received into stock and can no longer be edited.")

    def save(self, *args, **kwargs):
        self._check_draft()
        self.quantity_units = self.packs * self.units_per_pack
        self.line_total = (Decimal(self.packs) * self.purchase_price_per_pack).quantize(Decimal("0.01"))
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        self._check_draft()
        return super().delete(*args, **kwargs)


# ---------------------------------------------------------------- stock
class MedicineBatch(TimeStampedModel):
    medicine = models.ForeignKey(Medicine, on_delete=models.PROTECT, related_name="batches")
    supplier = models.ForeignKey(Supplier, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    purchase_item = models.OneToOneField(PurchaseInvoiceItem, null=True, blank=True, on_delete=models.PROTECT,
                                         related_name="batch")
    batch_number = models.CharField(max_length=40)
    expiry_date = models.DateField(db_index=True)
    received_date = models.DateField(default=timezone.localdate)
    quantity_received = models.PositiveIntegerField()
    quantity_on_hand = models.PositiveIntegerField()  # the database also refuses anything below zero
    purchase_price_per_unit = models.DecimalField(**UNIT_MONEY)
    selling_price_per_unit = models.DecimalField(**UNIT_MONEY)
    is_recalled = models.BooleanField(default=False)
    recall_reason = models.CharField(max_length=255, blank=True)
    recalled_at = models.DateTimeField(null=True, blank=True)
    recalled_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    created_by = models.ForeignKey(User, null=True, on_delete=models.PROTECT, related_name="+")

    class Meta:
        ordering = ["expiry_date", "received_date", "id"]
        indexes = [models.Index(fields=["medicine", "expiry_date"])]
        constraints = [models.CheckConstraint(condition=models.Q(quantity_on_hand__gte=0), name="batch_on_hand_non_negative")]

    def __str__(self):
        return f"{self.medicine.brand_name} batch {self.batch_number}"


class InventoryTransaction(ImmutableModel):
    """The stock ledger: append-only (also blocked by a database trigger). Every batch's quantity_on_hand
    must equal the sum of its ledger rows (check with `manage.py check_stock`)."""

    class Kind(models.TextChoices):
        RECEIPT = "RECEIPT", "Received from supplier"
        DISPENSE = "DISPENSE", "Dispensed"
        CUSTOMER_RETURN = "CUSTOMER_RETURN", "Returned by patient"
        SUPPLIER_RETURN = "SUPPLIER_RETURN", "Returned to supplier"
        WASTAGE = "WASTAGE", "Wastage / expired / damaged"
        ADJUSTMENT = "ADJUSTMENT", "Stock adjustment"

    batch = models.ForeignKey(MedicineBatch, on_delete=models.PROTECT, related_name="ledger")
    medicine = models.ForeignKey(Medicine, on_delete=models.PROTECT, related_name="+")
    kind = models.CharField(max_length=20, choices=Kind.choices)
    quantity_change = models.IntegerField()
    balance_after = models.PositiveIntegerField()
    unit_cost = models.DecimalField(**UNIT_MONEY)
    reason = models.CharField(max_length=255, blank=True)
    reference_type = models.CharField(max_length=40, blank=True)
    reference_id = models.CharField(max_length=40, blank=True)
    channel = models.CharField(max_length=20, blank=True, choices=SalesChannel.choices)  # for sales/returns
    created_by = models.ForeignKey(User, null=True, on_delete=models.PROTECT, related_name="+")
    created_at = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        constraints = [
            models.CheckConstraint(condition=~models.Q(quantity_change=0), name="ledger_change_not_zero"),
            models.CheckConstraint(
                condition=(models.Q(kind__in=["RECEIPT", "CUSTOMER_RETURN"], quantity_change__gt=0)
                           | models.Q(kind__in=["DISPENSE", "WASTAGE", "SUPPLIER_RETURN"], quantity_change__lt=0)
                           | models.Q(kind="ADJUSTMENT")),
                name="ledger_sign_matches_kind"),
        ]
