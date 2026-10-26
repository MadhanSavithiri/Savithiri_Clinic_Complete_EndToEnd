from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from apps.accounts.decorators import live_permission_required, permission_required
from apps.accounts.permissions import user_has_permission
from apps.core.audit import log_event
from apps.core.models import Setting

from . import services
from .forms import IdentifierForm, MergeConfirmForm, MergeLookupForm, PatientForm
from .models import Patient


def _consent_notice():
    return Setting.get("consent_notice_text", "")


@permission_required("patients.view")
def patient_list(request):
    q = request.GET.get("q", "").strip()
    return render(request, "patients/list.html", {"q": q, "patients": services.search_patients(q)})


@live_permission_required("patients.view")
def patient_search_partial(request):
    q = request.GET.get("q", "").strip()
    return render(request, "patients/_results.html", {"q": q, "patients": services.search_patients(q)})


@permission_required("patients.register")
def patient_create(request):
    form = PatientForm(request.POST or None)
    duplicates = []
    if request.method == "POST" and form.is_valid():
        data = form.cleaned_data
        if not request.POST.get("confirm_new"):
            duplicates = services.find_possible_duplicates(
                full_name=data["full_name"], phone=data["phone"], dob=data["date_of_birth"])
        if not duplicates:
            patient = services.register_patient(actor=request.user, request=request, data=data)
            messages.success(request, f"Registered {patient.full_name} with patient ID {patient.patient_id}.")
            return redirect("patients:detail", pk=patient.pk)
    return render(request, "patients/form.html", {
        "form": form, "creating": True, "duplicates": duplicates, "consent_notice": _consent_notice()})


@permission_required("patients.view")
def patient_detail(request, pk):
    patient = get_object_or_404(Patient, pk=pk)
    log_event(user=request.user, action="patient.view", obj=patient, request=request)
    from apps.queueing.models import Appointment, QueueToken  # local import: apps depend on patients, not vice versa
    today = timezone.localdate()
    return render(request, "patients/detail.html", {
        "patient": patient,
        "identifier_form": IdentifierForm(),
        "todays_token": QueueToken.objects.filter(
            patient=patient, token_date=today, status__in=QueueToken.OPEN_STATUSES).first(),
        "recent_tokens": QueueToken.objects.filter(patient=patient).order_by("-token_date", "-token_number")[:8],
        "upcoming": Appointment.objects.filter(
            patient=patient, status=Appointment.Status.SCHEDULED, scheduled_date__gte=today).order_by("scheduled_date")[:5],
        "can_queue": user_has_permission(request.user, "queue.add") and patient.is_active,
    })


@permission_required("patients.edit")
def patient_edit(request, pk):
    patient = get_object_or_404(Patient, pk=pk)
    if not patient.is_active:
        messages.error(request, "This record was merged into another patient and cannot be edited.")
        return redirect("patients:detail", pk=pk)
    form = PatientForm(request.POST or None, instance=patient)
    if request.method == "POST" and form.is_valid():
        try:
            services.update_patient(actor=request.user, request=request, patient=patient, data=form.cleaned_data)
        except services.PatientError as exc:
            messages.error(request, str(exc))
        else:
            messages.success(request, "Patient details saved.")
            return redirect("patients:detail", pk=pk)
    return render(request, "patients/form.html", {
        "form": form, "creating": False, "patient": patient, "duplicates": [], "consent_notice": _consent_notice()})


@permission_required("patients.edit")
@require_POST
def identifier_add(request, pk):
    patient = get_object_or_404(Patient, pk=pk, is_active=True)
    form = IdentifierForm(request.POST)
    if form.is_valid():
        try:
            services.add_identifier(actor=request.user, request=request, patient=patient, **form.cleaned_data)
        except services.PatientError as exc:
            messages.error(request, str(exc))
        else:
            messages.success(request, "Identifier added.")
    else:
        messages.error(request, "Enter an identifier type and value.")
    return redirect("patients:detail", pk=pk)


@permission_required("patients.merge")
def patient_merge(request, pk):
    target = get_object_or_404(Patient, pk=pk, is_active=True)
    form = MergeLookupForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        code = form.cleaned_data["other_patient_id"].strip()
        source = Patient.objects.filter(patient_id__iexact=code, is_active=True).exclude(pk=target.pk).first()
        if source is None:
            form.add_error("other_patient_id", "No current patient has that ID (or it is the same record).")
        else:
            return redirect("patients:merge_confirm", pk=target.pk, source_pk=source.pk)
    return render(request, "patients/merge.html", {"target": target, "form": form})


@permission_required("patients.merge")
def patient_merge_confirm(request, pk, source_pk):
    target = get_object_or_404(Patient, pk=pk, is_active=True)
    source = get_object_or_404(Patient, pk=source_pk, is_active=True)
    form = MergeConfirmForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        try:
            services.merge_patients(actor=request.user, request=request, target=target, source=source,
                                    reason=form.cleaned_data["reason"])
        except services.PatientError as exc:
            messages.error(request, str(exc))
        else:
            messages.success(request, f"{source.patient_id} was merged into {target.patient_id}.")
            return redirect("patients:detail", pk=target.pk)
    return render(request, "patients/merge_confirm.html", {"target": target, "source": source, "form": form})
