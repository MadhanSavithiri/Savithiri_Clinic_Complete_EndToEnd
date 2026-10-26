from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from apps.accounts.decorators import permission_required
from apps.accounts.permissions import user_has_permission
from apps.core.audit import log_event
from apps.patients.models import Patient
from apps.pharmacy.models import Medicine
from apps.queueing.views import _board_context

from . import bmi, services
from .forms import (AllergyForm, CurrentMedicineForm, DiagnosisForm, EncounterNotesForm, HistoryForm, NoteForm,
                    PrescriptionItemForm, ReasonForm, ReferralForm, VitalsForm)
from .models import (Allergy, Encounter, FollowUp, MedicalHistoryItem, Prescription, PrescriptionItem, RecordStatus)

VIEW = "clinical.view_notes"
WRITE = "clinical.write_notes"
PRESCRIBE = "clinical.prescribe"
CORRECT = "clinical.authorize_correction"
RX = Prescription.Status
FREQUENCY_HINTS = ["1-0-1", "1-1-1", "1-1-1-1", "1-0-0", "0-0-1", "Once daily", "Twice daily", "Three times daily",
                   "At bedtime", "SOS (only if needed)"]


# ---------------------------------------------------------------- helpers
def _enc(pk):
    return get_object_or_404(Encounter.objects.select_related("patient", "doctor", "queue_token"), pk=pk)


def _back(enc, anchor=""):
    return redirect(reverse("clinical:encounter", args=[enc.pk]) + (f"#{anchor}" if anchor else ""))


def _context(request, enc, **bound):
    user, patient = request.user, enc.patient
    in_progress = enc.status == Encounter.Status.IN_PROGRESS
    rxs = list(enc.prescriptions.prefetch_related("items").order_by("-version"))
    draft = next((r for r in rxs if r.status == RX.DRAFT), None)
    final = next((r for r in rxs if r.status == RX.FINAL), None)
    allergy_state, active_allergies = services.allergy_status(patient)
    can_write = user_has_permission(user, WRITE)
    can_prescribe = user_has_permission(user, PRESCRIBE)
    bmi_key, adult = bmi.standard_key(), bmi.is_adult(patient)
    vitals = list(enc.vitals.select_related("recorded_by"))
    for v in vitals:
        v.bmi_text = bmi.bmi_text(v.bmi, adult, bmi_key)
    ctx = {
        "bmi_cut": bmi.cutoffs(bmi_key), "bmi_adult": adult,
        "enc": enc, "patient": patient, "in_progress": in_progress,
        "editable": in_progress and can_write,
        "can_write": can_write, "can_prescribe": can_prescribe,
        "can_correct": user_has_permission(user, CORRECT),
        "allergy_state": allergy_state, "active_allergies": active_allergies,
        "allergy_history": patient.allergies.exclude(status=RecordStatus.ACTIVE),
        "history_items": patient.history_items.filter(status=RecordStatus.ACTIVE),
        "current_meds": patient.current_medicines.filter(status=RecordStatus.ACTIVE),
        "vitals": vitals,
        "diagnoses": enc.diagnoses.all(),
        "rxs": rxs, "draft": draft, "final": final,
        "draft_editable": bool(draft and can_prescribe and (in_progress or draft.supersedes_id)),
        "show_item_form": bool(can_prescribe and ((draft and (in_progress or draft.supersedes_id)) or (in_progress and not final))),
        "alerts": services.allergy_alerts(draft) if draft else [],
        "catalogue_empty": not Medicine.objects.filter(is_active=True).exists(),
        "previous": Encounter.objects.filter(patient=patient).exclude(pk=enc.pk)
                    .prefetch_related("diagnoses", "vitals").order_by("-started_at")[:8],
        "referrals": enc.referrals.all(), "notes": enc.notes.select_related("author"),
        "revisions": enc.revisions.select_related("revised_by"),
        "follow_up": FollowUp.objects.filter(encounter=enc).first(),
        "frequency_hints": FREQUENCY_HINTS,
        "forms": {
            "notes_form": EncounterNotesForm(instance=enc), "vitals_form": VitalsForm(), "dx_form": DiagnosisForm(),
            "allergy_form": AllergyForm(), "history_form": HistoryForm(), "med_form": CurrentMedicineForm(),
            "item_form": PrescriptionItemForm(), "referral_form": ReferralForm(), "note_form": NoteForm(),
        },
    }
    ctx["forms"].update(bound)
    ctx.update(ctx["forms"])
    return ctx


def _page(request, enc, **bound):
    return render(request, "clinical/encounter.html", _context(request, enc, **bound))


def _handle(request, pk, form_class, form_key, call, success, anchor):
    enc = _enc(pk)
    form = form_class(request.POST)
    if form.is_valid():
        try:
            call(enc, form.cleaned_data)
        except services.ClinicalError as exc:
            messages.error(request, str(exc))
        else:
            messages.success(request, success)
            return _back(enc, anchor)
    return _page(request, enc, **{form_key: form})


def _reason_page(request, title, body, button, back):
    form = ReasonForm(request.POST or None)
    return form, render(request, "clinical/reason_form.html", {"form": form, "title": title, "body": body,
                                                                "button": button, "back": back})


# ---------------------------------------------------------------- dashboard, start, record
@permission_required(VIEW)
def dashboard(request):
    today = timezone.localdate()
    ctx = _board_context(request)
    ctx.update({
        "follow_ups": FollowUp.objects.filter(status=FollowUp.Status.PENDING, due_date__lte=today)
                      .select_related("patient")[:20],
        "recent": Encounter.objects.filter(status=Encounter.Status.COMPLETED).select_related("patient")
                  .order_by("-completed_at")[:10],
        "today": today,
    })
    return render(request, "clinical/dashboard.html", ctx)


@permission_required(WRITE)
@require_POST
def start(request, token_pk):
    try:
        enc = services.start_consultation(actor=request.user, request=request, token_id=token_pk)
    except services.ClinicalError as exc:
        messages.error(request, str(exc))
        return redirect("clinical:dashboard")
    return redirect("clinical:encounter", pk=enc.pk)


@permission_required(VIEW)
def patient_record(request, pk):
    patient = get_object_or_404(Patient, pk=pk)
    log_event(user=request.user, action="clinical.view_record", obj=patient, request=request)
    state, active = services.allergy_status(patient)
    return render(request, "clinical/patient_record.html", {
        "patient": patient, "allergy_state": state, "active_allergies": active,
        "history_items": patient.history_items.filter(status=RecordStatus.ACTIVE),
        "current_meds": patient.current_medicines.filter(status=RecordStatus.ACTIVE),
        "encounters": Encounter.objects.filter(patient=patient).prefetch_related("diagnoses", "prescriptions")
                      .order_by("-started_at")})


@permission_required(VIEW)
def encounter(request, pk):
    enc = _enc(pk)
    log_event(user=request.user, action="clinical.view", obj=enc, request=request)
    return _page(request, enc)


# ---------------------------------------------------------------- consultation content
@permission_required(WRITE)
@require_POST
def notes_save(request, pk):
    enc = _enc(pk)
    form = EncounterNotesForm(request.POST, instance=Encounter.objects.get(pk=pk))
    if form.is_valid():
        try:
            services.update_notes(actor=request.user, request=request, encounter_id=pk, data=form.cleaned_data)
        except services.ClinicalError as exc:
            messages.error(request, str(exc))
            return _page(request, enc)
        messages.success(request, "Notes saved.")
        return _back(enc, "notes")
    return _page(request, enc, notes_form=form)


@permission_required(WRITE)
@require_POST
def vitals_add(request, pk):
    return _handle(request, pk, VitalsForm, "vitals_form",
                   lambda e, d: services.record_vitals(actor=request.user, request=request, encounter_id=e.pk, data=d),
                   "Vitals recorded.", "vitals")


@permission_required(WRITE)
@require_POST
def vitals_void(request, pk, vital_pk):
    enc = _enc(pk)
    form = ReasonForm(request.POST)
    if not form.is_valid():
        messages.error(request, "Enter a reason for marking the reading as an error.")
    else:
        try:
            services.void_vitals(actor=request.user, request=request, vital_id=vital_pk, reason=form.cleaned_data["reason"])
            messages.success(request, "Reading marked as entered in error.")
        except services.ClinicalError as exc:
            messages.error(request, str(exc))
    return _back(enc, "vitals")


@permission_required(WRITE)
@require_POST
def diagnosis_add(request, pk):
    return _handle(request, pk, DiagnosisForm, "dx_form",
                   lambda e, d: services.add_diagnosis(actor=request.user, request=request, encounter_id=e.pk, **d),
                   "Diagnosis added.", "diagnosis")


@permission_required(WRITE)
@require_POST
def diagnosis_void(request, pk, dx_pk):
    enc = _enc(pk)
    try:
        services.void_diagnosis(actor=request.user, request=request, diagnosis_id=dx_pk, reason=request.POST.get("reason", ""))
        messages.success(request, "Diagnosis removed (kept in the record as entered in error).")
    except services.ClinicalError as exc:
        messages.error(request, str(exc))
    return _back(enc, "diagnosis")


@permission_required(WRITE)
@require_POST
def allergy_add(request, pk):
    return _handle(request, pk, AllergyForm, "allergy_form",
                   lambda e, d: services.add_allergy(actor=request.user, request=request, patient=e.patient, **d),
                   "Allergy recorded.", "allergies")


@permission_required(WRITE)
@require_POST
def allergy_nkda(request, pk):
    enc = _enc(pk)
    try:
        services.set_no_known_allergies(actor=request.user, request=request, patient=enc.patient)
        messages.success(request, "Recorded: no known allergies.")
    except services.ClinicalError as exc:
        messages.error(request, str(exc))
    return _back(enc, "allergies")


@permission_required(WRITE)
@require_POST
def history_add(request, pk):
    return _handle(request, pk, HistoryForm, "history_form",
                   lambda e, d: services.add_history_item(actor=request.user, request=request, patient=e.patient, **d),
                   "History item added.", "history")


@permission_required(WRITE)
@require_POST
def medicine_add(request, pk):
    return _handle(request, pk, CurrentMedicineForm, "med_form",
                   lambda e, d: services.add_current_medicine(actor=request.user, request=request, patient=e.patient, **d),
                   "Current medicine added.", "history")


@permission_required(WRITE)
@require_POST
def record_status(request, pk, kind, record_pk):
    enc = _enc(pk)
    if kind not in services.RECORD_MODELS:
        return redirect("clinical:encounter", pk=pk)
    model = services.RECORD_MODELS[kind]
    record = get_object_or_404(model, pk=record_pk, patient=enc.patient)
    try:
        services.set_record_status(actor=request.user, request=request, kind=kind, record_id=record.pk,
                                   status=request.POST.get("status", ""))
        messages.success(request, "Updated.")
    except services.ClinicalError as exc:
        messages.error(request, str(exc))
    return _back(enc, "allergies" if kind == "allergy" else "history")


@permission_required(WRITE)
@require_POST
def referral_add(request, pk):
    return _handle(request, pk, ReferralForm, "referral_form",
                   lambda e, d: services.add_referral(actor=request.user, request=request, encounter_id=e.pk, **d),
                   "Referral recorded.", "referral")


@permission_required(WRITE)
@require_POST
def note_add(request, pk):
    return _handle(request, pk, NoteForm, "note_form",
                   lambda e, d: services.add_note(actor=request.user, request=request, encounter_id=e.pk, text=d["text"]),
                   "Note added.", "notes-log")


@permission_required(WRITE)
@require_POST
def complete(request, pk):
    enc = _enc(pk)
    try:
        services.complete_consultation(actor=request.user, request=request, encounter_id=pk)
    except services.ClinicalError as exc:
        messages.error(request, str(exc))
        return _page(request, enc)
    messages.success(request, "Consultation completed.")
    return redirect("clinical:dashboard")


@permission_required(CORRECT)
def reopen(request, pk):
    enc = _enc(pk)
    back = reverse("clinical:encounter", args=[pk])
    form, page = _reason_page(request, "Reopen this consultation?",
                              "The current notes are saved as a snapshot, with your reason, and cannot be altered. "
                              "You can then edit and complete the consultation again.", "Reopen", back)
    if request.method == "POST" and form.is_valid():
        try:
            services.reopen_consultation(actor=request.user, request=request, encounter_id=pk,
                                         reason=form.cleaned_data["reason"])
        except services.ClinicalError as exc:
            messages.error(request, str(exc))
        else:
            messages.success(request, "Consultation reopened. A snapshot of the earlier version was saved.")
        return _back(enc)
    return page


# ---------------------------------------------------------------- prescriptions
@permission_required(PRESCRIBE)
@require_POST
def rx_item_add(request, pk):
    return _handle(request, pk, PrescriptionItemForm, "item_form",
                   lambda e, d: services.add_item(actor=request.user, request=request, encounter_id=e.pk, data=d),
                   "Medicine added to the draft prescription.", "prescription")


@permission_required(PRESCRIBE)
def rx_item_edit(request, pk, item_pk):
    enc = _enc(pk)
    item = get_object_or_404(PrescriptionItem, pk=item_pk, prescription__encounter=enc)
    form = PrescriptionItemForm(request.POST or None, instance=item)
    if request.method == "POST" and form.is_valid():
        try:
            services.update_item(actor=request.user, request=request, item_id=item.pk, data=form.cleaned_data)
        except services.ClinicalError as exc:
            messages.error(request, str(exc))
        else:
            messages.success(request, "Medicine updated.")
            return _back(enc, "prescription")
    return render(request, "clinical/item_edit.html", {
        "form": form, "enc": enc, "patient": enc.patient, "frequency_hints": FREQUENCY_HINTS})


@permission_required(PRESCRIBE)
@require_POST
def rx_item_remove(request, pk, item_pk):
    enc = _enc(pk)
    item = get_object_or_404(PrescriptionItem, pk=item_pk, prescription__encounter=enc)
    try:
        services.remove_item(actor=request.user, request=request, item_id=item.pk)
        messages.success(request, "Medicine removed from the draft.")
    except services.ClinicalError as exc:
        messages.error(request, str(exc))
    return _back(enc, "prescription")


@permission_required(PRESCRIBE)
@require_POST
def rx_discard(request, rx_pk):
    rx = get_object_or_404(Prescription, pk=rx_pk)
    enc = _enc(rx.encounter_id)
    try:
        services.discard_draft(actor=request.user, request=request, prescription_id=rx.pk)
        messages.success(request, "Draft prescription discarded.")
    except services.ClinicalError as exc:
        messages.error(request, str(exc))
    return _back(enc, "prescription")


@permission_required(PRESCRIBE)
def rx_finalize(request, rx_pk):
    rx = get_object_or_404(Prescription.objects.prefetch_related("items"), pk=rx_pk)
    enc = _enc(rx.encounter_id)
    alerts = services.allergy_alerts(rx)
    if request.method == "POST":
        try:
            services.finalize_prescription(actor=request.user, request=request, prescription_id=rx.pk,
                                           acknowledge=request.POST.get("acknowledge") == "on")
        except services.ClinicalError as exc:
            messages.error(request, str(exc))
        else:
            messages.success(request, "Prescription issued.")
            return _back(enc, "prescription")
    return render(request, "clinical/rx_finalize.html", {"rx": rx, "enc": enc, "patient": enc.patient, "alerts": alerts})


@permission_required(VIEW)
def rx_print(request, rx_pk):
    rx = get_object_or_404(Prescription.objects.select_related("encounter", "patient").prefetch_related("items"), pk=rx_pk)
    if rx.status == RX.DRAFT:
        messages.error(request, "A draft prescription cannot be printed. Issue it first.")
        return redirect("clinical:encounter", pk=rx.encounter_id)
    log_event(user=request.user, action="rx.print", obj=rx, summary=rx.number, request=request)
    return render(request, "clinical/rx_print.html", {"rx": rx, "patient": rx.patient, "enc": rx.encounter})


@permission_required(CORRECT)
def rx_correct(request, rx_pk):
    rx = get_object_or_404(Prescription, pk=rx_pk)
    back = reverse("clinical:encounter", args=[rx.encounter_id])
    form, page = _reason_page(request, f"Correct prescription {rx.number}?",
                              "A new draft copy is created for you to edit. The issued prescription stays valid "
                              "until you issue the corrected one, which then replaces it (the old one is kept as "
                              "'superseded').", "Create correction draft", back)
    if request.method == "POST" and form.is_valid():
        try:
            services.start_correction(actor=request.user, request=request, prescription_id=rx.pk,
                                      reason=form.cleaned_data["reason"])
        except services.ClinicalError as exc:
            messages.error(request, str(exc))
        else:
            messages.success(request, "Correction draft created. Edit it, then issue it.")
        return redirect(back + "#prescription")
    return page


@permission_required(CORRECT)
def rx_cancel(request, rx_pk):
    rx = get_object_or_404(Prescription, pk=rx_pk)
    back = reverse("clinical:encounter", args=[rx.encounter_id])
    form, page = _reason_page(request, f"Cancel prescription {rx.number}?",
                              "The prescription is marked cancelled and should not be dispensed. It stays in the record.",
                              "Cancel prescription", back)
    if request.method == "POST" and form.is_valid():
        try:
            services.cancel_prescription(actor=request.user, request=request, prescription_id=rx.pk,
                                         reason=form.cleaned_data["reason"])
        except services.ClinicalError as exc:
            messages.error(request, str(exc))
        else:
            messages.success(request, "Prescription cancelled.")
        return redirect(back + "#prescription")
    return page
