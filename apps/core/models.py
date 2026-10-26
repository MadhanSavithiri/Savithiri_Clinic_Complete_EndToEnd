from django.conf import settings
from django.db import models
from django.utils import timezone


class TimeStampedModel(models.Model):
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


class ClinicProfile(TimeStampedModel):
    """Single-row clinic details used on every printed document."""

    name = models.CharField(max_length=200, default="Savithiri Clinic")
    tagline = models.CharField(max_length=200, default="General Physician & Family Doctor")
    address = models.TextField(default="Peruvalanallur / Lalgudi, Tiruchirappalli District, Tamil Nadu")
    phone = models.CharField(max_length=50, blank=True)
    email = models.EmailField(blank=True)
    consultation_hours = models.CharField(max_length=200, blank=True)
    # Official identifiers: deliberately blank. Enter your own verified details.
    clinic_registration_number = models.CharField(max_length=100, blank=True)
    pharmacy_licence_number = models.CharField(max_length=100, blank=True)
    gstin = models.CharField("GSTIN (if applicable)", max_length=20, blank=True)

    class Meta:
        verbose_name = "Clinic profile"

    def save(self, *args, **kwargs):
        self.pk = 1  # singleton
        super().save(*args, **kwargs)

    @classmethod
    def load(cls):
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj

    def __str__(self):
        return self.name


class DoctorProfile(TimeStampedModel):
    """Doctor details for prescriptions. Nothing is pre-filled: enter verified details only."""

    full_name = models.CharField(max_length=200)
    qualifications = models.CharField(max_length=200, blank=True)
    registration_number = models.CharField(max_length=100, blank=True)
    registration_council = models.CharField(max_length=200, blank=True)
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="doctor_profile"
    )
    is_active = models.BooleanField(default=True)

    def __str__(self):
        return self.full_name


class Setting(TimeStampedModel):
    """Key/value configuration (tax rules, partial payments, etc.)."""

    key = models.CharField(max_length=100, unique=True)
    value = models.JSONField()
    description = models.CharField(max_length=255, blank=True)

    def __str__(self):
        return self.key

    @classmethod
    def get(cls, key, default=None):
        try:
            return cls.objects.get(key=key).value
        except cls.DoesNotExist:
            return default


class SalesChannel(models.TextChoices):
    """Every sale, payment, refund and stock movement keeps the channel it belongs to."""

    CONSULTATION = "CONSULTATION", "Clinic consultation"
    CLINIC_PHARMACY = "CLINIC_PHARMACY", "Clinic-linked pharmacy"
    INDEPENDENT_COUNTER = "INDEPENDENT_COUNTER", "Independent counter sale"


class DocType(models.TextChoices):
    PATIENT = "PATIENT", "Patient ID"
    APPOINTMENT = "APPOINTMENT", "Appointment"
    TOKEN = "TOKEN", "Queue token"
    PRESCRIPTION = "PRESCRIPTION", "Prescription"
    CONSULT_RECEIPT = "CONSULT_RECEIPT", "Consultation receipt"
    PHARMACY_INVOICE = "PHARMACY_INVOICE", "Pharmacy invoice"
    PURCHASE_INVOICE = "PURCHASE_INVOICE", "Purchase entry"
    COUNTER_SALE = "COUNTER_SALE", "Counter sale bill"
    CREDIT_NOTE = "CREDIT_NOTE", "Credit note"


class DocumentSequence(TimeStampedModel):
    """Counter per (document type, period). Always advanced through core.sequences.next_number()."""

    doc_type = models.CharField(max_length=40, choices=DocType.choices)
    period = models.CharField(max_length=20, blank=True, default="", help_text="e.g. 2026 or 20261015; blank = never resets")
    prefix = models.CharField(max_length=10, blank=True)
    padding = models.PositiveSmallIntegerField(default=6)
    next_value = models.PositiveBigIntegerField(default=1)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["doc_type", "period"], name="uniq_sequence_type_period"),
            models.CheckConstraint(condition=models.Q(next_value__gte=1), name="sequence_next_value_gte_1"),
        ]

    def __str__(self):
        return f"{self.doc_type}/{self.period or '-'} -> {self.next_value}"


class AuditImmutableError(Exception):
    """Raised when code tries to change or delete an audit record."""


class AuditQuerySet(models.QuerySet):
    def update(self, **kwargs):
        raise AuditImmutableError("Audit events cannot be updated.")

    def delete(self):
        raise AuditImmutableError("Audit events cannot be deleted.")

    def bulk_update(self, *args, **kwargs):
        raise AuditImmutableError("Audit events cannot be updated.")


class AuditEvent(models.Model):
    """Append-only history of who did what, when. Also protected by a PostgreSQL trigger."""

    objects = AuditQuerySet.as_manager()

    timestamp = models.DateTimeField(default=timezone.now, db_index=True)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    username = models.CharField(max_length=150, blank=True)  # snapshot
    action = models.CharField(max_length=60, db_index=True)
    object_type = models.CharField(max_length=100, blank=True)
    object_id = models.CharField(max_length=64, blank=True)
    object_repr = models.CharField(max_length=255, blank=True)
    summary = models.TextField(blank=True)
    changes = models.JSONField(default=dict, blank=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)

    class Meta:
        ordering = ["-timestamp", "-id"]
        indexes = [models.Index(fields=["object_type", "object_id"])]

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise AuditImmutableError("Audit events cannot be modified.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise AuditImmutableError("Audit events cannot be deleted.")

    def __str__(self):
        return f"{self.timestamp:%Y-%m-%d %H:%M:%S} {self.username or 'system'} {self.action}"


class ImmutableRecordError(Exception):
    """Raised when code tries to change or delete a record that must never change."""


class ImmutableQuerySet(models.QuerySet):
    def update(self, **kwargs):
        raise ImmutableRecordError("This record type cannot be updated.")

    def delete(self):
        raise ImmutableRecordError("This record type cannot be deleted.")

    def bulk_update(self, *args, **kwargs):
        raise ImmutableRecordError("This record type cannot be updated.")


class ImmutableModel(models.Model):
    """Append-only: can be created, never edited or deleted (also enforced by a database trigger)."""

    objects = ImmutableQuerySet.as_manager()

    class Meta:
        abstract = True

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ImmutableRecordError("This record cannot be modified.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ImmutableRecordError("This record cannot be deleted.")
