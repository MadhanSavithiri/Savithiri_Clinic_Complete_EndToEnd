from django.contrib.auth.models import AbstractUser
from django.db import models
from django.utils import timezone


class User(AbstractUser):
    """Clinic staff account. Accounts are deactivated, never deleted."""

    class Role(models.TextChoices):
        ADMIN = "admin", "Clinic Administrator"
        DOCTOR = "doctor", "Doctor"
        RECEPTION = "reception", "Receptionist / Cashier"
        PHARMACY = "pharmacy", "Pharmacy Operator (clinic pharmacist)"
        PHARMACY_MANAGER = "pharmacy_manager", "Pharmacy Manager"
        COUNTER = "counter", "Independent Pharmacy Counter Salesperson"

    role = models.CharField(max_length=20, choices=Role.choices, default=Role.RECEPTION)
    phone = models.CharField(max_length=20, blank=True)
    failed_login_count = models.PositiveSmallIntegerField(default=0)
    locked_until = models.DateTimeField(null=True, blank=True)
    must_change_password = models.BooleanField(default=False)

    def is_locked(self):
        return self.locked_until is not None and self.locked_until > timezone.now()

    def __str__(self):
        return f"{self.get_full_name() or self.username} ({self.get_role_display()})"
