from datetime import date

from django.conf import settings
from django.db import models

from apps.core.models import TimeStampedModel

from .normalize import normalize_name


class Sex(models.TextChoices):
    MALE = "M", "Male"
    FEMALE = "F", "Female"
    OTHER = "O", "Other"
    UNKNOWN = "U", "Not stated"


class Patient(TimeStampedModel):
    serial = models.PositiveBigIntegerField(unique=True)
    patient_id = models.CharField(max_length=30, unique=True)
    full_name = models.CharField(max_length=200)
    name_search = models.CharField(max_length=200, db_index=True, editable=False)
    sex = models.CharField(max_length=1, choices=Sex.choices, default=Sex.UNKNOWN)
    date_of_birth = models.DateField(null=True, blank=True)
    dob_estimated = models.BooleanField(default=False)
    phone = models.CharField(max_length=15, blank=True, db_index=True)
    address = models.TextField(blank=True)
    emergency_contact_name = models.CharField(max_length=200, blank=True)
    emergency_contact_phone = models.CharField(max_length=15, blank=True)
    emergency_contact_relation = models.CharField(max_length=100, blank=True)
    consent_given = models.BooleanField(default=False)
    consent_given_by = models.CharField("Consent given by", max_length=200, blank=True)
    consent_recorded_at = models.DateTimeField(null=True, blank=True)
    consent_recorded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    is_active = models.BooleanField(default=True)  # False once merged into another record
    merged_into = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.PROTECT, related_name="merged_from"
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.PROTECT, related_name="+"
    )

    class Meta:
        ordering = ["-serial"]
        constraints = [
            models.CheckConstraint(
                condition=(models.Q(is_active=True, merged_into__isnull=True)
                           | models.Q(is_active=False, merged_into__isnull=False)),
                name="patient_merge_state_consistent",
            ),
        ]

    def save(self, *args, **kwargs):
        self.name_search = normalize_name(self.full_name)
        super().save(*args, **kwargs)

    def __str__(self):
        return self.patient_id  # no name here: keeps personal data out of audit/log lines

    def age_display(self, today=None):
        if not self.date_of_birth:
            return "-"
        today = today or date.today()
        dob = self.date_of_birth
        years = today.year - dob.year - ((today.month, today.day) < (dob.month, dob.day))
        if years >= 2:
            text = f"{years} y"
        else:
            months = (today.year - dob.year) * 12 + today.month - dob.month - (today.day < dob.day)
            text = f"{max(months, 0)} m"
        return f"~{text}" if self.dob_estimated else text


class PatientIdentifier(TimeStampedModel):
    class IdType(models.TextChoices):
        ABHA = "ABHA", "ABHA number"
        OLD_RECORD = "OLD_RECORD", "Old clinic record no."
        SCHEME = "SCHEME", "Insurance / scheme ID"
        OTHER = "OTHER", "Other"

    patient = models.ForeignKey(Patient, on_delete=models.PROTECT, related_name="identifiers")
    id_type = models.CharField(max_length=20, choices=IdType.choices)
    value = models.CharField(max_length=64)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.PROTECT, related_name="+")

    class Meta:
        constraints = [models.UniqueConstraint(fields=["id_type", "value"], name="uniq_identifier_type_value")]

    def __str__(self):
        return f"{self.get_id_type_display()}: {self.value}"


class PatientMergeLog(TimeStampedModel):
    source = models.ForeignKey(Patient, on_delete=models.PROTECT, related_name="merge_logs_as_source")
    target = models.ForeignKey(Patient, on_delete=models.PROTECT, related_name="merge_logs_as_target")
    merged_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    reason = models.CharField(max_length=255)
    moved_counts = models.JSONField(default=dict)
