from decimal import Decimal

from django.conf import settings
from django.core.validators import MinValueValidator
from django.db import models
from django.utils import timezone

from apps.core.models import ImmutableModel, SalesChannel, TimeStampedModel

User = settings.AUTH_USER_MODEL
MONEY = {"max_digits": 12, "decimal_places": 2}
ZERO = Decimal("0.00")


class Desk(models.TextChoices):
    COUNTER = "COUNTER", "Independent counter"
    CLINIC_PHARMACY = "CLINIC_PHARMACY", "Clinic pharmacy"
    RECEPTION = "RECEPTION", "Reception"


class PaymentMethod(models.TextChoices):
    CASH = "CASH", "Cash"
    UPI = "UPI", "UPI"
    CARD = "CARD", "Card"
    OTHER = "OTHER", "Other"


class Shift(TimeStampedModel):
    """A cashier's working period. Cash in the drawer is reconciled when the shift is closed."""

    user = models.ForeignKey(User, on_delete=models.PROTECT, related_name="+")
    desk = models.CharField(max_length=20, choices=Desk.choices)
    opened_at = models.DateTimeField(default=timezone.now)
    opening_cash = models.DecimalField(default=ZERO, validators=[MinValueValidator(ZERO)], **MONEY)
    closed_at = models.DateTimeField(null=True, blank=True)
    expected_cash = models.DecimalField(null=True, blank=True, **MONEY)
    counted_cash = models.DecimalField(null=True, blank=True, **MONEY)
    variance = models.DecimalField(null=True, blank=True, **MONEY)
    close_notes = models.CharField(max_length=255, blank=True)

    class Meta:
        ordering = ["-opened_at"]
        constraints = [
            models.UniqueConstraint(fields=["user", "desk"], condition=models.Q(closed_at__isnull=True),
                                    name="one_open_shift_per_user_desk"),
            models.CheckConstraint(condition=models.Q(opening_cash__gte=0), name="shift_opening_cash_non_negative"),
        ]

    @property
    def is_open(self):
        return self.closed_at is None


class SaleLockedError(Exception):
    """A completed sale cannot be edited. Use a return / credit note instead."""


class Sale(TimeStampedModel):
    class Status(models.TextChoices):
        DRAFT = "DRAFT", "Open bill"
        COMPLETED = "COMPLETED", "Completed"

    channel = models.CharField(max_length=20, choices=SalesChannel.choices)
    number = models.CharField(max_length=40, null=True, blank=True, unique=True)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.DRAFT)
    cashier = models.ForeignKey(User, on_delete=models.PROTECT, related_name="+")
    shift = models.ForeignKey(Shift, null=True, blank=True, on_delete=models.PROTECT, related_name="sales")
    customer_name = models.CharField(max_length=150, blank=True)
    customer_phone = models.CharField(max_length=15, blank=True)
    # Only clinic-linked sales (Phase 7B) may point at a patient or prescription. The database enforces it.
    patient = models.ForeignKey("patients.Patient", null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    prescription = models.ForeignKey("clinical.Prescription", null=True, blank=True, on_delete=models.PROTECT,
                                     related_name="+")
    subtotal = models.DecimalField(default=ZERO, **MONEY)
    discount_percent = models.DecimalField(max_digits=5, decimal_places=2, default=ZERO)
    discount_total = models.DecimalField(default=ZERO, **MONEY)
    discount_reason = models.CharField(max_length=255, blank=True)
    total = models.DecimalField(default=ZERO, **MONEY)
    amount_paid = models.DecimalField(default=ZERO, **MONEY)
    # prescription-only sales (Schedule H / H1 / other restricted)
    rx_prescriber_name = models.CharField(max_length=150, blank=True)
    rx_prescriber_reg_no = models.CharField(max_length=60, blank=True)
    rx_patient_name = models.CharField(max_length=150, blank=True)
    rx_approved_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    rx_approved_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    receipt_prints = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["-id"]
        constraints = [
            models.UniqueConstraint(fields=["cashier", "channel"], condition=models.Q(status="DRAFT"),
                                    name="one_open_bill_per_cashier_channel"),
            models.CheckConstraint(condition=models.Q(status="DRAFT") | models.Q(number__isnull=False),
                                   name="completed_sale_has_number"),
            models.CheckConstraint(
                condition=(models.Q(channel="INDEPENDENT_COUNTER", patient__isnull=True, prescription__isnull=True)
                           | models.Q(channel="CLINIC_PHARMACY")),
                name="counter_sale_is_never_linked_to_a_patient"),
            models.CheckConstraint(condition=models.Q(subtotal__gte=0, discount_total__gte=0, total__gte=0),
                                   name="sale_amounts_non_negative"),
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
            raise SaleLockedError("This sale is completed and can no longer be edited.")
        super().save(*args, **kwargs)
        self._loaded_status = self.status

    def delete(self, *args, **kwargs):
        if self._is_locked() or self.status != self.Status.DRAFT:
            raise SaleLockedError("Only an open bill can be discarded.")
        return super().delete(*args, **kwargs)

    def __str__(self):
        return self.number or f"Open bill {self.pk}"


class SaleLine(TimeStampedModel):
    """While the bill is open: one line per medicine (no batch yet). At completion the lines are replaced by
    one line per batch actually used, with the price and cost recorded as they were that day."""

    sale = models.ForeignKey(Sale, on_delete=models.CASCADE, related_name="lines")
    medicine = models.ForeignKey("pharmacy.Medicine", on_delete=models.PROTECT, related_name="+")
    batch = models.ForeignKey("pharmacy.MedicineBatch", null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    description = models.CharField(max_length=250, blank=True)
    batch_number = models.CharField(max_length=40, blank=True)
    expiry_date = models.DateField(null=True, blank=True)
    quantity = models.PositiveIntegerField(validators=[MinValueValidator(1)])
    unit_price = models.DecimalField(max_digits=12, decimal_places=4, default=Decimal("0"))
    unit_cost = models.DecimalField(max_digits=12, decimal_places=4, null=True, blank=True)  # never shown at the counter
    line_total = models.DecimalField(default=ZERO, **MONEY)
    discount_amount = models.DecimalField(default=ZERO, **MONEY)

    class Meta:
        ordering = ["id"]
        constraints = [
            models.CheckConstraint(condition=models.Q(quantity__gte=1), name="sale_line_quantity_positive"),
            models.UniqueConstraint(fields=["sale", "medicine"], condition=models.Q(batch__isnull=True),
                                    name="one_open_line_per_medicine"),
        ]

    def _check_open(self):
        if self.sale.status != Sale.Status.DRAFT:
            raise SaleLockedError("This sale is completed and can no longer be edited.")

    def save(self, *args, **kwargs):
        self._check_open()
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        self._check_open()
        return super().delete(*args, **kwargs)

    @property
    def net_total(self):
        return self.line_total - self.discount_amount


class Payment(ImmutableModel):
    """Money in (PAYMENT) or out (REFUND). Never edited or deleted: a mistake is a new, opposite row."""

    class Kind(models.TextChoices):
        PAYMENT = "PAYMENT", "Payment"
        REFUND = "REFUND", "Refund"

    sale = models.ForeignKey(Sale, on_delete=models.PROTECT, related_name="payments")
    channel = models.CharField(max_length=20, choices=SalesChannel.choices)
    kind = models.CharField(max_length=10, choices=Kind.choices, default=Kind.PAYMENT)
    method = models.CharField(max_length=10, choices=PaymentMethod.choices)
    amount = models.DecimalField(**MONEY)
    reference = models.CharField(max_length=60, blank=True)
    shift = models.ForeignKey(Shift, null=True, blank=True, on_delete=models.PROTECT, related_name="payments")
    received_by = models.ForeignKey(User, on_delete=models.PROTECT, related_name="+")
    created_at = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        ordering = ["created_at", "id"]
        constraints = [models.CheckConstraint(condition=models.Q(amount__gt=0), name="payment_amount_positive")]
