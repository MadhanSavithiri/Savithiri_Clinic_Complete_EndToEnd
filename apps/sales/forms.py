from django import forms

from apps.patients.normalize import normalize_phone

from . import services
from .models import PaymentMethod


class AddToCartForm(forms.Form):
    medicine_id = forms.IntegerField(min_value=1, widget=forms.HiddenInput)
    quantity = forms.IntegerField(min_value=1, initial=1)


class QuantityForm(forms.Form):
    quantity = forms.IntegerField(min_value=0)


class OpenShiftForm(forms.Form):
    opening_cash = forms.DecimalField(label="Cash in the drawer now (\u20b9)", min_value=0, max_digits=12,
                                      decimal_places=2, initial=0)


class CloseShiftForm(forms.Form):
    counted_cash = forms.DecimalField(label="Cash counted in the drawer (\u20b9)", min_value=0, max_digits=12, decimal_places=2)
    notes = forms.CharField(label="Notes (optional)", required=False, max_length=255)


class CheckoutForm(forms.Form):
    sale_id = forms.IntegerField(widget=forms.HiddenInput)
    expected_subtotal = forms.DecimalField(required=False, max_digits=12, decimal_places=2, widget=forms.HiddenInput)
    customer_name = forms.CharField(label="Customer name (optional)", required=False, max_length=150)
    customer_phone = forms.CharField(label="Customer phone (optional)", required=False, max_length=20)
    discount_percent = forms.DecimalField(label="Discount %", required=False, min_value=0, max_value=100, max_digits=5,
                                          decimal_places=2, initial=0)
    discount_reason = forms.CharField(label="Reason for discount", required=False, max_length=255)
    pay1_method = forms.ChoiceField(label="Payment method")
    pay1_amount = forms.DecimalField(label="Amount (\u20b9)", required=False, min_value=0, max_digits=12, decimal_places=2)
    pay1_reference = forms.CharField(label="UPI / card reference (optional)", required=False, max_length=60)
    pay1_confirmed = forms.BooleanField(label="UPI / card payment received", required=False)
    pay2_method = forms.ChoiceField(label="Second payment (split bill)", required=False)
    pay2_amount = forms.DecimalField(label="Amount (\u20b9)", required=False, min_value=0, max_digits=12, decimal_places=2)
    pay2_reference = forms.CharField(label="Reference (optional)", required=False, max_length=60)
    pay2_confirmed = forms.BooleanField(label="UPI / card payment received", required=False)
    rx_prescriber_name = forms.CharField(label="Prescriber's name", required=False, max_length=150)
    rx_prescriber_reg_no = forms.CharField(label="Prescriber's registration no.", required=False, max_length=60)
    rx_patient_name = forms.CharField(label="Patient's name (on the prescription)", required=False, max_length=150)
    approver_username = forms.CharField(label="Pharmacist username", required=False, max_length=150)
    approver_password = forms.CharField(label="Pharmacist password", required=False,
                                        widget=forms.PasswordInput(render_value=False, attrs={"autocomplete": "new-password"}))

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        methods = [(m, PaymentMethod(m).label) for m in services.enabled_methods()]
        self.fields["pay1_method"].choices = methods
        self.fields["pay2_method"].choices = [("", "None")] + methods

    def clean_customer_phone(self):
        phone = normalize_phone(self.cleaned_data.get("customer_phone", ""))
        if phone is None:
            raise forms.ValidationError("Enter a 10-digit number, or leave this blank.")
        return phone

    def clean(self):
        c = super().clean()
        if not c.get("pay1_amount"):
            self.add_error("pay1_amount", "Enter the amount received.")
        if c.get("pay2_amount") and not c.get("pay2_method"):
            self.add_error("pay2_method", "Choose the method for the second payment.")
        return c

    def payments(self):
        c, out = self.cleaned_data, []
        out.append({"method": c["pay1_method"], "amount": c["pay1_amount"], "reference": c.get("pay1_reference", ""),
                    "confirmed": c.get("pay1_confirmed", False)})
        if c.get("pay2_amount") and c.get("pay2_method"):
            out.append({"method": c["pay2_method"], "amount": c["pay2_amount"], "reference": c.get("pay2_reference", ""),
                        "confirmed": c.get("pay2_confirmed", False)})
        return out

    def restricted(self):
        c = self.cleaned_data
        return {"prescriber_name": c.get("rx_prescriber_name", ""), "prescriber_reg_no": c.get("rx_prescriber_reg_no", ""),
                "patient_name": c.get("rx_patient_name", ""), "approver_username": c.get("approver_username", ""),
                "approver_password": c.get("approver_password", "")}
