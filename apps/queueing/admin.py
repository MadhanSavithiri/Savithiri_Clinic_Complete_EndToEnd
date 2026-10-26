from django.contrib import admin

from apps.patients.admin import ReadOnlyAdmin

from .models import Appointment, QueueToken


@admin.register(Appointment)
class AppointmentAdmin(ReadOnlyAdmin):
    list_display = ("number", "patient", "kind", "status", "scheduled_date")
    list_filter = ("status", "kind")


@admin.register(QueueToken)
class QueueTokenAdmin(ReadOnlyAdmin):
    list_display = ("code", "patient", "status", "token_date", "registered_at")
    list_filter = ("status", "token_date")
