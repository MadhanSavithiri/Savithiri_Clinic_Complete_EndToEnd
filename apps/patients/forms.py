from datetime import date

from django import forms

from .models import Patient, PatientIdentifier
from .normalize import normalize_phone


def estimate_dob(age_years, today=None):
    today = today or date.today()
    try:
        return today.replace(year=today.year - age_years)
    except ValueError:  # 29 Feb
        return today.replace(year=today.year - age_years, day=28)


class PatientForm(forms.ModelForm):
    age_years = forms.IntegerField(
        label="Age in years", min_value=0, max_value=120, required=False,
        help_text="Use only if the date of birth is not known.",
    )

    field_order = [
        "full_name", "sex", "date_of_birth", "age_years", "phone", "address",
        "emergency_contact_name", "emergency_contact_phone", "emergency_contact_relation",
        "consent_given", "consent_given_by",
    ]

    class Meta:
        model = Patient
        fields = [
            "full_name", "sex", "date_of_birth", "phone", "address",
            "emergency_contact_name", "emergency_contact_phone", "emergency_contact_relation",
            "consent_given", "consent_given_by",
        ]
        widgets = {
            "date_of_birth": forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"),
            "address": forms.Textarea(attrs={"rows": 3}),
        }
        labels = {"consent_given": "Consent recorded"}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["phone"].help_text = "10-digit number. Leave blank if the patient has no phone."
        self.fields["emergency_contact_phone"].help_text = "10-digit number, optional."
        self.fields["consent_given_by"].help_text = "Patient, or the guardian's name if the patient is a minor."
        if self.instance.pk and self.instance.dob_estimated:
            self.fields["date_of_birth"].help_text = "Estimated from age."

    def _clean_phone(self, name):
        value = normalize_phone(self.cleaned_data.get(name, ""))
        if value is None:
            raise forms.ValidationError("Enter a 10-digit number, or leave this blank.")
        return value

    def clean_phone(self):
        return self._clean_phone("phone")

    def clean_emergency_contact_phone(self):
        return self._clean_phone("emergency_contact_phone")

    def clean_full_name(self):
        name = " ".join(self.cleaned_data["full_name"].split())
        if len(name) < 2:
            raise forms.ValidationError("Enter the patient's full name.")
        return name

    def clean(self):
        cleaned = super().clean()
        dob, age = cleaned.get("date_of_birth"), cleaned.get("age_years")
        estimated = False
        if dob:
            if dob > date.today():
                self.add_error("date_of_birth", "Date of birth cannot be in the future.")
            if self.instance.pk and dob == self.instance.date_of_birth:
                estimated = self.instance.dob_estimated
        elif age is not None:
            cleaned["date_of_birth"] = estimate_dob(age)
            estimated = True
        elif "date_of_birth" not in self.errors:
            self.add_error("age_years", "Enter the date of birth or the age.")
        cleaned["dob_estimated"] = estimated
        if cleaned.get("consent_given") and not cleaned.get("consent_given_by", "").strip():
            self.add_error("consent_given_by", "Say who gave the consent (patient or guardian).")
        return cleaned


class IdentifierForm(forms.ModelForm):
    class Meta:
        model = PatientIdentifier
        fields = ["id_type", "value"]

    def clean_value(self):
        return self.cleaned_data["value"].strip().upper()


class MergeLookupForm(forms.Form):
    other_patient_id = forms.CharField(label="Patient ID of the duplicate record", max_length=30)


class MergeConfirmForm(forms.Form):
    reason = forms.CharField(label="Reason for merging", max_length=255)
    confirm = forms.BooleanField(label="I understand this cannot be undone from the screen")
