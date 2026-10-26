from decimal import Decimal

from django import forms
from django.core.validators import MinValueValidator
from django.utils import timezone

from apps.core.models import Setting
from apps.pharmacy.models import Medicine

from .rxcalc import calc_quantity

from .models import (Allergy, ClinicalNote, CurrentMedicine, Diagnosis, Encounter, MedicalHistoryItem,
                     PrescriptionItem, Referral, VITAL_LIMITS)

TA = {"rows": 3}


class ScopedIds:
    """Several forms share one page, so each gets its own HTML id prefix (no duplicate ids, labels stay linked)."""

    id_scope = ""

    def __init__(self, *args, **kwargs):
        if self.id_scope:
            kwargs.setdefault("auto_id", f"id_{self.id_scope}_%s")
        super().__init__(*args, **kwargs)


class EncounterNotesForm(ScopedIds, forms.ModelForm):
    id_scope = "notes"
    class Meta:
        model = Encounter
        fields = Encounter.NOTE_FIELDS
        widgets = {
            "presenting_complaints": forms.Textarea(attrs={"rows": 3}),
            "examination_findings": forms.Textarea(attrs={"rows": 3}),
            "assessment": forms.Textarea(attrs={"rows": 2}),
            "treatment_plan": forms.Textarea(attrs={"rows": 2}),
            "advice": forms.Textarea(attrs={"rows": 2}),
            "follow_up_date": forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"),
        }

    def clean_follow_up_date(self):
        d = self.cleaned_data.get("follow_up_date")
        if d and d < timezone.localdate():
            raise forms.ValidationError("The follow-up date cannot be in the past.")
        return d


def _dec(field):
    lo, hi = VITAL_LIMITS[field]
    return forms.DecimalField(required=False, min_value=lo, max_value=hi, max_digits=5, decimal_places=1,
                              widget=forms.NumberInput(attrs={"step": "0.1"}))


def _int(field):
    lo, hi = VITAL_LIMITS[field]
    return forms.IntegerField(required=False, min_value=lo, max_value=hi)


class VitalsForm(ScopedIds, forms.Form):
    id_scope = "vitals"
    height_cm = _dec("height_cm")
    weight_kg = _dec("weight_kg")
    systolic = _int("systolic")
    diastolic = _int("diastolic")
    pulse = _int("pulse")
    temperature = forms.DecimalField(required=False, max_digits=5, decimal_places=1, min_value=Decimal("30"),
                                     max_value=Decimal("120"), widget=forms.NumberInput(attrs={"step": "0.1"}))
    temperature_unit = forms.ChoiceField(choices=[("F", "°F"), ("C", "°C")], initial="F")
    spo2 = _int("spo2")
    respiratory_rate = _int("respiratory_rate")
    other_observations = forms.CharField(required=False, max_length=255, label="Other observations")

    labels = {"height_cm": "Height (cm)", "weight_kg": "Weight (kg)", "systolic": "BP systolic (mmHg)",
              "diastolic": "BP diastolic (mmHg)", "pulse": "Pulse (/min)", "temperature": "Temperature",
              "spo2": "SpO2 (%)", "respiratory_rate": "Respiratory rate (/min)"}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for name, label in self.labels.items():
            self.fields[name].label = label

    def clean(self):
        c = super().clean()
        if self.errors:
            return c
        sys_, dia = c.get("systolic"), c.get("diastolic")
        if (sys_ is None) != (dia is None):
            raise forms.ValidationError("Enter both systolic and diastolic blood pressure, or neither.")
        if sys_ is not None and sys_ <= dia:
            raise forms.ValidationError("Systolic pressure must be higher than diastolic.")
        temp = c.get("temperature")
        c["temperature_c"] = None
        if temp is not None:
            celsius = temp if c["temperature_unit"] == "C" else (temp - 32) * 5 / 9
            celsius = Decimal(celsius).quantize(Decimal("0.1"))
            lo, hi = VITAL_LIMITS["temperature_c"]
            if not (lo <= celsius <= hi):
                raise forms.ValidationError("Temperature looks wrong (expected about 86-113 °F or 30-45 °C).")
            c["temperature_c"] = celsius
        values = [c.get(k) for k in ("height_cm", "weight_kg", "systolic", "pulse", "temperature_c", "spo2",
                                      "respiratory_rate")] + [c.get("other_observations")]
        if not any(v not in (None, "") for v in values):
            raise forms.ValidationError("Enter at least one observation.")
        return c


class DiagnosisForm(ScopedIds, forms.ModelForm):
    id_scope = "dx"
    class Meta:
        model = Diagnosis
        fields = ["description", "code", "is_primary"]
        labels = {"description": "Diagnosis", "code": "Code (optional)", "is_primary": "Primary diagnosis"}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        system = Setting.get("diagnosis_code_system", "")
        self.fields["code"].help_text = f"Code system: {system}" if system else "Leave blank unless the clinic uses a code system."


class AllergyForm(ScopedIds, forms.ModelForm):
    id_scope = "allergy"
    class Meta:
        model = Allergy
        fields = ["allergen", "reaction", "severity"]
        labels = {"allergen": "Allergen (drug, food, other)"}


class HistoryForm(ScopedIds, forms.ModelForm):
    id_scope = "hist"
    class Meta:
        model = MedicalHistoryItem
        fields = ["kind", "description", "since"]


class CurrentMedicineForm(ScopedIds, forms.ModelForm):
    id_scope = "med"
    class Meta:
        model = CurrentMedicine
        fields = ["name", "dose_notes"]
        labels = {"name": "Medicine the patient takes now", "dose_notes": "Dose / notes"}


class ReferralForm(ScopedIds, forms.ModelForm):
    id_scope = "ref"
    class Meta:
        model = Referral
        fields = ["referred_to", "specialty", "reason"]
        widgets = {"reason": forms.Textarea(attrs=TA)}


class NoteForm(ScopedIds, forms.ModelForm):
    id_scope = "note"
    class Meta:
        model = ClinicalNote
        fields = ["text"]
        labels = {"text": "Note"}
        widgets = {"text": forms.Textarea(attrs=TA)}


class ReasonForm(forms.Form):
    reason = forms.CharField(label="Reason", max_length=255)


ROUTES = ["Oral", "Topical", "Injection (IM)", "Injection (IV)", "Injection (SC)", "Inhalation", "Eye", "Ear",
          "Nasal", "Sublingual", "Rectal", "Other"]


class PrescriptionItemForm(ScopedIds, forms.ModelForm):
    id_scope = "item"
    medicine = forms.ModelChoiceField(queryset=Medicine.objects.none(), required=False,
                                      empty_label="Other: type the name below", label="Medicine from catalogue")
    route = forms.ChoiceField(choices=[(r, r) for r in ROUTES], initial="Oral")
    meal_timing = forms.ChoiceField(
        label="Take", required=False, widget=forms.RadioSelect,
        choices=[("", "Not specified"), ("AFTER", "After food"), ("BEFORE", "Before food")])

    class Meta:
        model = PrescriptionItem
        fields = ["medicine", "medicine_name", "strength", "dosage_form", "dose", "route", "frequency", "meal_timing",
                  "duration", "quantity", "instructions"]
        labels = {"medicine_name": "Medicine name (if not in catalogue)", "dose": "Dose (e.g. 1 tablet, 5 mL)",
                  "frequency": "Frequency", "duration": "Duration (e.g. 5 days)", "quantity": "Total quantity to supply"}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["medicine"].queryset = Medicine.objects.filter(is_active=True)
        self.fields["medicine"].label_from_instance = lambda m: m.label_with_generic
        self.fields["medicine_name"].required = False
        self.fields["frequency"].widget.attrs["list"] = "freq-list"
        self.fields["quantity"].required = False  # filled in automatically from frequency and duration
        self.fields["quantity"].validators.append(MinValueValidator(1))
        self.fields["quantity"].widget.attrs["min"] = 1
        self.fields["quantity"].help_text = "Worked out from dose, frequency and duration. You can change it."
        if self.instance.pk and self.instance.route not in ROUTES:
            self.fields["route"].choices = [(self.instance.route, self.instance.route)] + self.fields["route"].choices

    def clean(self):
        c = super().clean()
        med = c.get("medicine")
        if med:
            c["medicine_name"] = med.brand_name
            c["strength"] = c.get("strength") or med.strength
            c["dosage_form"] = c.get("dosage_form") or med.dosage_form
        elif not (c.get("medicine_name") or "").strip():
            self.add_error("medicine_name", "Choose a medicine from the catalogue or type its name.")
        if not c.get("quantity") and "quantity" not in self.errors:
            result = calc_quantity(c.get("dose", ""), c.get("frequency", ""), c.get("duration", ""))
            if result.quantity:
                c["quantity"] = result.quantity
            else:
                self.add_error("quantity", result.explanation)
        return c
