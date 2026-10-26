from datetime import date as date_cls

from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.html import format_html
from django.views.decorators.http import require_POST

from apps.accounts.decorators import live_permission_required, permission_required
from apps.accounts.permissions import user_has_permission
from apps.patients.models import Patient

from . import services
from .forms import AppointmentForm, ReasonForm
from .models import Appointment, QueueToken

S = QueueToken.Status


def _board_context(request, today=None):
    today = today or timezone.localdate()
    tokens = list(QueueToken.objects.filter(token_date=today).select_related("patient", "encounter").order_by("token_number"))
    counts = {s.value: 0 for s in S}
    for t in tokens:
        counts[t.status] += 1
    return {
        "today": today,
        "active": [t for t in tokens if t.status in QueueToken.OPEN_STATUSES],
        "finished": [t for t in tokens if t.status not in QueueToken.OPEN_STATUSES],
        "counts": counts,
        "total": len(tokens),
        "can_consult": user_has_permission(request.user, "queue.consult"),
        "can_cancel": user_has_permission(request.user, "queue.cancel"),
    }


@permission_required("queue.view")
def queue_page(request):
    return render(request, "queueing/queue.html", _board_context(request))


@live_permission_required("queue.view")
def queue_partial(request):
    return render(request, "queueing/_board.html", _board_context(request))


@permission_required("queue.add")
@require_POST
def walk_in(request, patient_pk):
    patient = get_object_or_404(Patient, pk=patient_pk)
    try:
        token = services.register_walk_in(actor=request.user, request=request, patient=patient)
    except services.QueueError as exc:
        messages.error(request, str(exc))
        return redirect("patients:detail", pk=patient.pk)
    messages.success(request, format_html(
        'Token {} issued to {}. <a href="{}" target="_blank" rel="noopener">Print token slip</a>',
        token.token_number, patient.full_name, reverse("queueing:token_print", args=[token.pk])))
    return redirect("queueing:queue")


def _token_action(action):
    permission = services.TOKEN_ACTIONS[action][2]

    @permission_required(permission)
    @require_POST
    def view(request, pk):
        token = get_object_or_404(QueueToken, pk=pk)
        try:
            services.change_token_status(actor=request.user, request=request, token_id=token.pk, action=action)
        except services.QueueError as exc:
            messages.error(request, str(exc))
        return redirect("queueing:queue")

    view.__name__ = f"token_{action}"
    return view


token_call = _token_action("call")
token_return = _token_action("return")
token_complete = _token_action("complete")


@permission_required("queue.cancel")
def token_cancel(request, pk):
    token = get_object_or_404(QueueToken.objects.select_related("patient"), pk=pk)
    form = ReasonForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        try:
            services.change_token_status(actor=request.user, request=request, token_id=token.pk,
                                         action="cancel", reason=form.cleaned_data["reason"])
        except services.QueueError as exc:
            messages.error(request, str(exc))
        else:
            messages.success(request, f"Token {token.token_number} cancelled.")
        return redirect("queueing:queue")
    return render(request, "queueing/cancel_confirm.html", {
        "form": form, "title": f"Cancel token {token.token_number} for {token.patient.full_name}?",
        "back": reverse("queueing:queue")})


@permission_required("queue.view")
def token_print(request, pk):
    token = get_object_or_404(QueueToken.objects.select_related("patient"), pk=pk)
    return render(request, "queueing/token_print.html", {"token": token})


@permission_required("queue.view")
def appointment_list(request):
    try:
        day = date_cls.fromisoformat(request.GET.get("date", ""))
    except ValueError:
        day = timezone.localdate()
    appts = Appointment.objects.filter(scheduled_date=day).select_related("patient")
    return render(request, "queueing/appointment_list.html", {
        "day": day, "appointments": appts, "is_today": day == timezone.localdate(),
        "can_add": user_has_permission(request.user, "queue.add"),
        "can_cancel": user_has_permission(request.user, "queue.cancel")})


@permission_required("queue.add")
def appointment_create(request, patient_pk):
    patient = get_object_or_404(Patient, pk=patient_pk, is_active=True)
    form = AppointmentForm(request.POST or None, initial={"date": timezone.localdate()})
    if request.method == "POST" and form.is_valid():
        try:
            appt = services.book_appointment(actor=request.user, request=request, patient=patient, **form.cleaned_data)
        except services.QueueError as exc:
            messages.error(request, str(exc))
        else:
            messages.success(request, f"Appointment {appt.number} booked for {appt.scheduled_date:%d/%m/%Y}.")
            return redirect(f"{reverse('queueing:appointment_list')}?date={appt.scheduled_date.isoformat()}")
    return render(request, "queueing/appointment_form.html", {"form": form, "patient": patient})


@permission_required("queue.add")
@require_POST
def appointment_checkin(request, pk):
    try:
        token = services.check_in_appointment(actor=request.user, request=request, appointment_id=get_object_or_404(Appointment, pk=pk).pk)
    except services.QueueError as exc:
        messages.error(request, str(exc))
        return redirect("queueing:appointment_list")
    messages.success(request, format_html(
        'Checked in: token {}. <a href="{}" target="_blank" rel="noopener">Print token slip</a>',
        token.token_number, reverse("queueing:token_print", args=[token.pk])))
    return redirect("queueing:queue")


@permission_required("queue.cancel")
def appointment_cancel(request, pk):
    appt = get_object_or_404(Appointment.objects.select_related("patient"), pk=pk)
    form = ReasonForm(request.POST or None)
    back = f"{reverse('queueing:appointment_list')}?date={appt.scheduled_date.isoformat()}"
    if request.method == "POST" and form.is_valid():
        try:
            services.cancel_appointment(actor=request.user, request=request, appointment_id=appt.pk,
                                        reason=form.cleaned_data["reason"])
        except services.QueueError as exc:
            messages.error(request, str(exc))
        else:
            messages.success(request, f"Appointment {appt.number} cancelled.")
        return redirect(back)
    return render(request, "queueing/cancel_confirm.html", {
        "form": form, "title": f"Cancel appointment {appt.number} for {appt.patient.full_name}?", "back": back})
