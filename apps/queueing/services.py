"""Queue and appointment rules. Atomic, audited, safe if two workstations click at the same moment."""
from django.core.exceptions import PermissionDenied
from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.accounts.permissions import user_has_permission
from apps.core.audit import log_event
from apps.core.models import DocType
from apps.core.sequences import next_number

from .models import Appointment, QueueToken

Status = QueueToken.Status


class QueueError(Exception):
    """A rule was broken; the message is safe to show to staff."""


def _require(actor, code):
    if not user_has_permission(actor, code):
        raise PermissionDenied


def add_to_queue(*, actor, request, patient, appointment=None, today=None):
    _require(actor, "queue.add")
    today = today or timezone.localdate()
    if not patient.is_active:
        raise QueueError("This record was merged into another patient. Use the current record.")
    existing = QueueToken.objects.filter(patient=patient, token_date=today, status__in=QueueToken.OPEN_STATUSES).first()
    if existing:
        raise QueueError(f"{patient.full_name} is already in today's queue (token {existing.token_number}).")
    try:
        with transaction.atomic():  # counter + token commit together, so a failure wastes no number
            number = next_number(DocType.TOKEN, period=f"{today:%Y%m%d}")
            token = QueueToken.objects.create(
                patient=patient, appointment=appointment, token_date=today, token_number=number.value,
                code=number.formatted, status=Status.WAITING, registered_at=timezone.now(), created_by=actor)
            log_event(user=actor, action="queue.add", obj=token,
                      summary=f"Token {token.token_number} for {patient.patient_id}", request=request)
    except IntegrityError:
        raise QueueError("This patient was just added by another workstation. Refresh the queue.")
    return token


def register_walk_in(*, actor, request, patient, reason="", today=None):
    _require(actor, "queue.add")
    today = today or timezone.localdate()
    with transaction.atomic():
        number = next_number(DocType.APPOINTMENT, period=f"{today:%Y}")
        appt = Appointment.objects.create(
            number=number.formatted, patient=patient, kind=Appointment.Kind.WALK_IN,
            status=Appointment.Status.CHECKED_IN, scheduled_date=today, reason=reason, created_by=actor)
        token = add_to_queue(actor=actor, request=request, patient=patient, appointment=appt, today=today)
    return token


def book_appointment(*, actor, request, patient, date, time=None, reason="", today=None):
    _require(actor, "queue.add")
    today = today or timezone.localdate()
    if not patient.is_active:
        raise QueueError("This record was merged into another patient. Use the current record.")
    if date < today:
        raise QueueError("An appointment cannot be booked for a past date.")
    with transaction.atomic():
        number = next_number(DocType.APPOINTMENT, period=f"{today:%Y}")
        appt = Appointment.objects.create(
            number=number.formatted, patient=patient, kind=Appointment.Kind.BOOKED,
            scheduled_date=date, scheduled_time=time, reason=reason, created_by=actor)
        log_event(user=actor, action="appointment.book", obj=appt,
                  summary=f"{appt.number} for {patient.patient_id} on {date}", request=request)
    return appt


def check_in_appointment(*, actor, request, appointment_id, today=None):
    _require(actor, "queue.add")
    today = today or timezone.localdate()
    with transaction.atomic():
        appt = Appointment.objects.select_for_update(of=("self",)).select_related("patient").get(pk=appointment_id)
        if appt.status != Appointment.Status.SCHEDULED:
            raise QueueError("This appointment is no longer waiting to be checked in.")
        if appt.scheduled_date != today:
            raise QueueError(f"This appointment is for {appt.scheduled_date:%d/%m/%Y}, not today.")
        token = add_to_queue(actor=actor, request=request, patient=appt.patient, appointment=appt, today=today)
        appt.status = Appointment.Status.CHECKED_IN
        appt.save(update_fields=["status", "updated_at"])
        log_event(user=actor, action="appointment.check_in", obj=appt, request=request)
    return token


def cancel_appointment(*, actor, request, appointment_id, reason):
    _require(actor, "queue.cancel")
    reason = (reason or "").strip()
    if not reason:
        raise QueueError("Enter a reason for cancelling.")
    with transaction.atomic():
        appt = Appointment.objects.select_for_update().get(pk=appointment_id)
        if appt.status != Appointment.Status.SCHEDULED:
            raise QueueError("Only appointments that are still scheduled can be cancelled.")
        appt.status, appt.cancel_reason = Appointment.Status.CANCELLED, reason
        appt.save(update_fields=["status", "cancel_reason", "updated_at"])
        log_event(user=actor, action="appointment.cancel", obj=appt, summary=reason, request=request)
    return appt


# action -> (required current status, new status, permission)
TOKEN_ACTIONS = {
    "call": (Status.WAITING, Status.IN_CONSULTATION, "queue.consult"),
    "return": (Status.IN_CONSULTATION, Status.WAITING, "queue.consult"),
    "complete": (Status.IN_CONSULTATION, Status.COMPLETED, "queue.consult"),
    "cancel": (Status.WAITING, Status.CANCELLED, "queue.cancel"),
}


def change_token_status(*, actor, request, token_id, action, reason=""):
    required_from, new_status, permission = TOKEN_ACTIONS[action]
    _require(actor, permission)
    reason = (reason or "").strip()
    if action == "cancel" and not reason:
        raise QueueError("Enter a reason for cancelling.")
    now = timezone.now()
    with transaction.atomic():
        token = QueueToken.objects.select_for_update(of=("self",)).select_related("appointment").get(pk=token_id)
        if token.status != required_from:
            raise QueueError(f"Token {token.token_number} is now '{token.get_status_display()}'. "
                             "Someone else changed it. Check the queue.")
        old = token.status
        token.status = new_status
        if action == "call":
            token.called_at = now
        elif action == "return":
            token.called_at = None
        elif action == "complete":
            token.completed_at = now
        elif action == "cancel":
            token.cancelled_at, token.cancel_reason = now, reason
            if token.appointment_id:
                appt = token.appointment
                appt.status, appt.cancel_reason = Appointment.Status.CANCELLED, reason
                appt.save(update_fields=["status", "cancel_reason", "updated_at"])
        token.save()
        log_event(user=actor, action=f"queue.{action}", obj=token, summary=reason,
                  changes={"status": [old, new_status]}, request=request)
    return token
