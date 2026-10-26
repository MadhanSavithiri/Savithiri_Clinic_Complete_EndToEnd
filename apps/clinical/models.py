from decimal import ROUND_HALF_UP, Decimal

from django.conf import settings
from django.db import models
from django.utils import timezone

from apps.core.models import ImmutableModel, TimeStampedModel

User = settings.AUTH_USER_MODEL


class RecordStatus(models.TextChoices):
    ACTIVE = "ACTIVE", "Active"
    RESOLVED = "RESOLVED", "Resolved / stopped"
    ENTERED_IN_ERROR = "ERROR", "Entered in error"


# ---- patient-level clinical information ------------------------------------
class ClinicalProfile(TimeStampedModel):
    patient = models.OneToOneField("patients.Patient", on_delete=models.PROTECT, related_name="clinical_profile")
    no_known_allergies = models.BooleanField(default=False)
    allergies_reviewed_at = models.DateTimeField(null=True, blank=True)
    allergies_reviewed_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.PROTECT, related_name="+")


class Allergy(TimeStampedModel):
    class Severity(models.TextChoices):
        MILD = "MILD", "Mild"
        MODERATE = "MODERATE", "Moderate"
        SEVERE = "SEVERE", "Severe"
        UNKNOWN = "UNKNOWN", "Not known"

    patient = models.ForeignKey("patients.Patient", on_delete=models.PROTECT, related_name="allergies")
    allergen = models.CharField(max_length=120)
    reaction = models.CharField(max_length=200, blank=True)
    severity = models.CharField(max_length=10, choices=Severity.choices, default=Severity.UNKNOWN)
    status = models.CharField(max_length=10, choices=RecordStatus.choices, default=RecordStatus.ACTIVE)
    recorded_by = models.ForeignKey(User, null=True, on_delete=models.PROTECT, related_name="+")

    class Meta:
        ordering = ["-created_at"]


class MedicalHistoryItem(TimeStampedModel):
    class Kind(models.TextChoices):
        CONDITION = "CONDITION", "Medical condition"
        SURGERY = "SURGERY", "Surgery / procedure"
        FAMILY = "FAMILY", "Family history"
        OTHER = "OTHER", "Other"

    patient = models.ForeignKey("patients.Patient", on_delete=models.PROTECT, related_name="history_items")
    kind = models.CharField(max_length=12, choices=Kind.choices, default=Kind.CONDITION)
    description = models.CharField(max_length=255)
    since = models.CharField(max_length=40, blank=True, help_text="e.g. 2019 or 'childhood'")
    status = models.CharField(max_length=10, choices=RecordStatus.choices, default=RecordStatus.ACTIVE)
    recorded_by = models.ForeignKey(User, null=True, on_delete=models.PROTECT, related_name="+")

    class Meta:
        ordering = ["kind", "-created_at"]


class CurrentMedicine(TimeStampedModel):
    """Medicines the patient says they take now (from any source). Not the clinic's prescriptions."""

    patient = models.ForeignKey("patients.Patient", on_delete=models.PROTECT, related_name="current_medicines")
    name = models.CharField(max_length=150)
    dose_notes = models.CharField(max_length=200, blank=True)
    status = models.CharField(max_length=10, choices=RecordStatus.choices, default=RecordStatus.ACTIVE)
    recorded_by = models.ForeignKey(User, null=True, on_delete=models.PROTECT, related_name="+")

    class Meta:
        ordering = ["-created_at"]


# ---- the visit --------------------------------------------------------------
class Encounter(TimeStampedModel):
    class Status(models.TextChoices):
        IN_PROGRESS = "IN_PROGRESS", "In progress"
        COMPLETED = "COMPLETED", "Completed"

    patient = models.ForeignKey("patients.Patient", on_delete=models.PROTECT, related_name="encounters")
    queue_token = models.OneToOneField("queueing.QueueToken", null=True, blank=True, on_delete=models.PROTECT,
                                       related_name="encounter")
    doctor = models.ForeignKey(User, on_delete=models.PROTECT, related_name="+")
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.IN_PROGRESS)
    started_at = models.DateTimeField(default=timezone.now)
    completed_at = models.DateTimeField(null=True, blank=True)
    presenting_complaints = models.TextField("Presenting complaints", blank=True)
    symptom_duration = models.CharField("Duration of symptoms", max_length=100, blank=True)
    examination_findings = models.TextField("Examination findings", blank=True)
    assessment = models.TextField("Assessment", blank=True)
    treatment_plan = models.TextField("Treatment plan", blank=True)
    advice = models.TextField("Advice to patient", blank=True)
    follow_up_date = models.DateField("Follow-up date", null=True, blank=True)
    follow_up_notes = models.CharField("Follow-up note", max_length=200, blank=True)

    NOTE_FIELDS = ["presenting_complaints", "symptom_duration", "examination_findings", "assessment",
                   "treatment_plan", "advice", "follow_up_date", "follow_up_notes"]

    class Meta:
        ordering = ["-started_at"]
        indexes = [models.Index(fields=["patient", "-started_at"])]

    def __str__(self):
        return f"Visit {self.pk}"


class EncounterRevision(ImmutableModel):
    """Snapshot saved whenever a completed consultation is reopened for correction."""

    encounter = models.ForeignKey(Encounter, on_delete=models.PROTECT, related_name="revisions")
    revised_by = models.ForeignKey(User, on_delete=models.PROTECT, related_name="+")
    reason = models.CharField(max_length=255)
    snapshot = models.JSONField()
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["created_at"]


class ClinicalNote(ImmutableModel):
    """Addenda and extra notes. Never edited or deleted; a correction is a new note."""

    class Kind(models.TextChoices):
        ADDENDUM = "ADDENDUM", "Addendum"
        GENERAL = "GENERAL", "Note"

    encounter = models.ForeignKey(Encounter, on_delete=models.PROTECT, related_name="notes")
    author = models.ForeignKey(User, on_delete=models.PROTECT, related_name="+")
    kind = models.CharField(max_length=10, choices=Kind.choices, default=Kind.GENERAL)
    text = models.TextField()
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["created_at"]


# Plausibility limits (catch typing slips such as 1200 for 120). One source for the DB and the forms.
VITAL_LIMITS = {
    "height_cm": (Decimal("20"), Decimal("250")),
    "weight_kg": (Decimal("0.3"), Decimal("400")),
    "systolic": (40, 300),
    "diastolic": (20, 200),
    "pulse": (20, 300),
    "temperature_c": (Decimal("30"), Decimal("45")),
    "spo2": (50, 100),
    "respiratory_rate": (4, 80),
}


def _range_constraint(field):
    lo, hi = VITAL_LIMITS[field]
    return models.CheckConstraint(
        condition=models.Q(**{f"{field}__isnull": True}) | models.Q(**{f"{field}__gte": lo, f"{field}__lte": hi}),
        name=f"vital_{field}_in_range",
    )


class VitalObservation(TimeStampedModel):
    encounter = models.ForeignKey(Encounter, on_delete=models.PROTECT, related_name="vitals")
    recorded_by = models.ForeignKey(User, on_delete=models.PROTECT, related_name="+")
    recorded_at = models.DateTimeField(default=timezone.now)
    height_cm = models.DecimalField(max_digits=4, decimal_places=1, null=True, blank=True)
    weight_kg = models.DecimalField(max_digits=4, decimal_places=1, null=True, blank=True)
    systolic = models.PositiveSmallIntegerField(null=True, blank=True)
    diastolic = models.PositiveSmallIntegerField(null=True, blank=True)
    pulse = models.PositiveSmallIntegerField(null=True, blank=True)
    temperature_c = models.DecimalField(max_digits=3, decimal_places=1, null=True, blank=True)
    spo2 = models.PositiveSmallIntegerField(null=True, blank=True)
    respiratory_rate = models.PositiveSmallIntegerField(null=True, blank=True)
    other_observations = models.CharField(max_length=255, blank=True)
    is_void = models.BooleanField(default=False)  # entered in error: kept, shown struck-through, never edited
    void_reason = models.CharField(max_length=255, blank=True)
    voided_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.PROTECT, related_name="+")

    class Meta:
        ordering = ["recorded_at", "id"]
        constraints = [_range_constraint(f) for f in VITAL_LIMITS] + [
            models.CheckConstraint(
                condition=(models.Q(systolic__isnull=True) | models.Q(diastolic__isnull=True)
                           | models.Q(systolic__gt=models.F("diastolic"))),
                name="vital_systolic_above_diastolic"),
        ]

    @property
    def bmi(self):
        if self.height_cm and self.weight_kg:
            m = self.height_cm / Decimal(100)
            return (self.weight_kg / (m * m)).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)
        return None

    @property
    def temperature_f(self):
        if self.temperature_c is None:
            return None
        return (self.temperature_c * Decimal(9) / Decimal(5) + Decimal(32)).quantize(Decimal("0.1"))


class Diagnosis(TimeStampedModel):
    encounter = models.ForeignKey(Encounter, on_delete=models.PROTECT, related_name="diagnoses")
    description = models.CharField(max_length=255)
    code = models.CharField(max_length=20, blank=True)
    code_system = models.CharField(max_length=30, blank=True)
    is_primary = models.BooleanField(default=False)
    is_void = models.BooleanField(default=False)
    void_reason = models.CharField(max_length=255, blank=True)
    recorded_by = models.ForeignKey(User, on_delete=models.PROTECT, related_name="+")

    class Meta:
        ordering = ["-is_primary", "id"]
        verbose_name_plural = "diagnoses"


# ---- prescriptions ------------------------------------------------------------
class PrescriptionLockedError(Exception):
    """A finalized prescription cannot be edited. Create a correction (new version) instead."""


class Prescription(TimeStampedModel):
    class Status(models.TextChoices):
        DRAFT = "DRAFT", "Draft"
        FINAL = "FINAL", "Issued"
        SUPERSEDED = "SUPERSEDED", "Superseded by a correction"
        CANCELLED = "CANCELLED", "Cancelled"

    encounter = models.ForeignKey(Encounter, on_delete=models.PROTECT, related_name="prescriptions")
    patient = models.ForeignKey("patients.Patient", on_delete=models.PROTECT, related_name="prescriptions")
    number = models.CharField(max_length=40, null=True, blank=True, unique=True)
    version = models.PositiveSmallIntegerField(default=1)
    supersedes = models.ForeignKey("self", null=True, blank=True, on_delete=models.PROTECT, related_name="superseded_by")
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.DRAFT)
    correction_reason = models.CharField(max_length=255, blank=True)
    cancel_reason = models.CharField(max_length=255, blank=True)
    created_by = models.ForeignKey(User, on_delete=models.PROTECT, related_name="+")
    finalized_at = models.DateTimeField(null=True, blank=True)
    finalized_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    # Doctor details copied at the moment of issue, so later profile edits never change old prescriptions.
    doctor_name = models.CharField(max_length=200, blank=True)
    doctor_qualifications = models.CharField(max_length=200, blank=True)
    doctor_registration_number = models.CharField(max_length=100, blank=True)
    doctor_registration_council = models.CharField(max_length=200, blank=True)
    alerts_acknowledged = models.BooleanField(default=False)
    alert_summary = models.TextField(blank=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(fields=["encounter"], condition=models.Q(status="DRAFT"), name="one_draft_rx_per_encounter"),
            models.UniqueConstraint(fields=["encounter"], condition=models.Q(status="FINAL"), name="one_final_rx_per_encounter"),
            models.CheckConstraint(condition=models.Q(status="DRAFT") | models.Q(number__isnull=False),
                                   name="issued_rx_has_number"),
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
            raise PrescriptionLockedError("This prescription has been issued and can no longer be edited.")
        super().save(*args, **kwargs)
        self._loaded_status = self.status

    def delete(self, *args, **kwargs):
        if self._is_locked() or self.status != self.Status.DRAFT:
            raise PrescriptionLockedError("Only a draft prescription can be discarded.")
        return super().delete(*args, **kwargs)

    def mark(self, status, **fields):
        """The ONLY way an issued prescription changes: its status (superseded / cancelled)."""
        type(self).objects.filter(pk=self.pk).update(status=status, updated_at=timezone.now(), **fields)
        self.status = status
        self._loaded_status = status
        for key, value in fields.items():
            setattr(self, key, value)

    @property
    def is_final(self):
        return self.status == self.Status.FINAL

    def __str__(self):
        return self.number or f"Draft {self.pk}"


class PrescriptionItem(TimeStampedModel):
    prescription = models.ForeignKey(Prescription, on_delete=models.CASCADE, related_name="items")
    line_no = models.PositiveSmallIntegerField(default=1)
    medicine = models.ForeignKey("pharmacy.Medicine", null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    medicine_name = models.CharField(max_length=200)
    strength = models.CharField(max_length=60, blank=True)
    dosage_form = models.CharField(max_length=30, blank=True)
    dose = models.CharField(max_length=100)
    route = models.CharField(max_length=40, default="Oral")
    frequency = models.CharField(max_length=100)
    meal_timing = models.CharField(max_length=6, blank=True,
                                   choices=[("AFTER", "After food"), ("BEFORE", "Before food")])
    duration = models.CharField(max_length=60)
    quantity = models.PositiveIntegerField()
    instructions = models.CharField(max_length=255, blank=True)

    class Meta:
        ordering = ["line_no", "id"]
        constraints = [models.CheckConstraint(condition=models.Q(quantity__gte=1), name="rx_item_quantity_positive")]

    @property
    def meal_text(self):
        return dict(self._meta.get_field("meal_timing").choices).get(self.meal_timing, "")

    def save(self, *args, **kwargs):
        if self.prescription.status != Prescription.Status.DRAFT:
            raise PrescriptionLockedError("This prescription has been issued and can no longer be edited.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        if self.prescription.status != Prescription.Status.DRAFT:
            raise PrescriptionLockedError("This prescription has been issued and can no longer be edited.")
        return super().delete(*args, **kwargs)


# ---- follow-up and referral ------------------------------------------------------
class FollowUp(TimeStampedModel):
    class Status(models.TextChoices):
        PENDING = "PENDING", "Pending"
        DONE = "DONE", "Done"
        CANCELLED = "CANCELLED", "Cancelled"

    patient = models.ForeignKey("patients.Patient", on_delete=models.PROTECT, related_name="follow_ups")
    encounter = models.OneToOneField(Encounter, on_delete=models.PROTECT, related_name="follow_up")
    due_date = models.DateField(db_index=True)
    notes = models.CharField(max_length=200, blank=True)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.PENDING)
    created_by = models.ForeignKey(User, on_delete=models.PROTECT, related_name="+")

    class Meta:
        ordering = ["due_date"]


class Referral(TimeStampedModel):
    patient = models.ForeignKey("patients.Patient", on_delete=models.PROTECT, related_name="referrals")
    encounter = models.ForeignKey(Encounter, on_delete=models.PROTECT, related_name="referrals")
    referred_to = models.CharField("Referred to (doctor / hospital)", max_length=200)
    specialty = models.CharField(max_length=100, blank=True)
    reason = models.TextField()
    created_by = models.ForeignKey(User, on_delete=models.PROTECT, related_name="+")

    class Meta:
        ordering = ["-created_at"]
