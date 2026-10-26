from django.conf import settings
from django.db import models

from apps.core.models import TimeStampedModel


class Appointment(TimeStampedModel):
    class Kind(models.TextChoices):
        WALK_IN = "WALK_IN", "Walk-in"
        BOOKED = "BOOKED", "Booked"

    class Status(models.TextChoices):
        SCHEDULED = "SCHEDULED", "Scheduled"
        CHECKED_IN = "CHECKED_IN", "Checked in"
        CANCELLED = "CANCELLED", "Cancelled"

    number = models.CharField(max_length=40, unique=True)
    patient = models.ForeignKey("patients.Patient", on_delete=models.PROTECT)
    kind = models.CharField(max_length=10, choices=Kind.choices)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.SCHEDULED)
    scheduled_date = models.DateField(db_index=True)
    scheduled_time = models.TimeField(null=True, blank=True)
    reason = models.CharField("Reason for visit", max_length=200, blank=True)
    cancel_reason = models.CharField(max_length=255, blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.PROTECT, related_name="+")

    class Meta:
        ordering = ["scheduled_date", "scheduled_time", "id"]

    def __str__(self):
        return self.number


class QueueToken(TimeStampedModel):
    class Status(models.TextChoices):
        WAITING = "WAITING", "Waiting"
        IN_CONSULTATION = "IN_CONSULTATION", "In consultation"
        COMPLETED = "COMPLETED", "Completed"
        CANCELLED = "CANCELLED", "Cancelled"

    OPEN_STATUSES = ("WAITING", "IN_CONSULTATION")

    patient = models.ForeignKey("patients.Patient", on_delete=models.PROTECT)
    appointment = models.ForeignKey(Appointment, null=True, blank=True, on_delete=models.PROTECT)
    token_date = models.DateField(db_index=True)
    token_number = models.PositiveIntegerField()
    code = models.CharField(max_length=40)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.WAITING)
    registered_at = models.DateTimeField()
    called_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    cancelled_at = models.DateTimeField(null=True, blank=True)
    cancel_reason = models.CharField(max_length=255, blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.PROTECT, related_name="+")

    class Meta:
        ordering = ["token_date", "token_number"]
        constraints = [
            models.UniqueConstraint(fields=["token_date", "token_number"], name="uniq_token_per_day"),
            # A patient can have only ONE open token per day (database-enforced, safe under double clicks).
            models.UniqueConstraint(
                fields=["patient", "token_date"],
                condition=models.Q(status__in=["WAITING", "IN_CONSULTATION"]),
                name="uniq_open_token_per_patient_day",
            ),
        ]

    def __str__(self):
        return self.code
