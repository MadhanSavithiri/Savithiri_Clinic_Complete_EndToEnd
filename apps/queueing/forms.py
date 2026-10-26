from django import forms
from django.utils import timezone


class AppointmentForm(forms.Form):
    date = forms.DateField(label="Date", widget=forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"))
    time = forms.TimeField(label="Time (optional)", required=False, widget=forms.TimeInput(attrs={"type": "time"}, format="%H:%M"))
    reason = forms.CharField(label="Reason for visit (optional)", required=False, max_length=200,
                             help_text="Do not enter clinical details here.")

    def clean_date(self):
        d = self.cleaned_data["date"]
        if d < timezone.localdate():
            raise forms.ValidationError("Choose today or a future date.")
        return d


class ReasonForm(forms.Form):
    reason = forms.CharField(label="Reason", max_length=255)
