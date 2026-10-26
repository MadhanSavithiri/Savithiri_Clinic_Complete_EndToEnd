from decimal import Decimal

from django import forms
from django.utils import timezone

from .models import Medicine, PurchaseInvoice, PurchaseInvoiceItem, Supplier


class MedicineForm(forms.ModelForm):
    class Meta:
        model = Medicine
        fields = ["item_kind", "brand_name", "generic_name", "strength", "dosage_form", "manufacturer",
                  "schedule_category", "unit", "pack_size", "reorder_threshold", "default_supplier",
                  "item_code", "rack", "shelf", "drawer", "box", "storage_notes", "counter_sale_allowed",
                  "notes", "is_active"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["default_supplier"].queryset = Supplier.objects.filter(is_active=True)

    def clean(self):
        cleaned = super().clean()
        for f in ("brand_name", "generic_name", "strength", "manufacturer", "unit"):
            if f in cleaned:
                cleaned[f] = " ".join(cleaned[f].split())
        for f in ("item_code", "rack", "shelf", "drawer", "box"):  # codes are kept upper-case: r02 -> R02
            if f in cleaned:
                cleaned[f] = cleaned[f].strip().upper()
        return cleaned


class SupplierForm(forms.ModelForm):
    class Meta:
        model = Supplier
        fields = ["name", "contact_person", "phone", "address", "gstin", "drug_licence_number", "notes", "is_active"]
        widgets = {"address": forms.Textarea(attrs={"rows": 2})}

    def clean_name(self):
        name = " ".join(self.cleaned_data["name"].split())
        clash = Supplier.objects.filter(name__iexact=name).exclude(pk=self.instance.pk)
        if clash.exists():
            raise forms.ValidationError("A supplier with this name already exists.")
        return name


class PurchaseHeaderForm(forms.ModelForm):
    class Meta:
        model = PurchaseInvoice
        fields = ["supplier", "supplier_invoice_number", "invoice_date", "discount_amount", "tax_amount", "notes"]
        widgets = {"invoice_date": forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d")}
        labels = {"supplier_invoice_number": "Supplier's invoice number", "discount_amount": "Discount (as on the invoice)"}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["supplier"].queryset = Supplier.objects.filter(is_active=True)
        self.fields["tax_amount"].help_text = "Copy the tax total from the invoice. The system does not calculate tax."

    def clean_invoice_date(self):
        d = self.cleaned_data["invoice_date"]
        if d > timezone.localdate():
            raise forms.ValidationError("The invoice date cannot be in the future.")
        return d


class PurchaseItemForm(forms.ModelForm):
    units_per_pack = forms.IntegerField(min_value=1, required=False, label="Units per pack",
                                        help_text="Leave blank to use the catalogue's pack size.")

    class Meta:
        model = PurchaseInvoiceItem
        fields = ["medicine", "batch_number", "expiry_date", "packs", "units_per_pack", "purchase_price_per_pack",
                  "mrp_per_pack"]
        widgets = {"expiry_date": forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d")}
        labels = {"purchase_price_per_pack": "Purchase price per pack", "mrp_per_pack": "Selling price (MRP) per pack"}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["medicine"].queryset = Medicine.objects.filter(is_active=True)
        self.fields["medicine"].label_from_instance = lambda m: m.label_with_generic

    def clean_expiry_date(self):
        d = self.cleaned_data["expiry_date"]
        if d <= timezone.localdate():
            raise forms.ValidationError("This date has passed. Expired goods cannot be received.")
        return d

    def clean(self):
        c = super().clean()
        med = c.get("medicine")
        if med and not c.get("units_per_pack"):
            c["units_per_pack"] = med.pack_size
        return c


class ReasonForm(forms.Form):
    reason = forms.CharField(label="Reason", max_length=255)


class QuantityReasonForm(ReasonForm):
    quantity = forms.IntegerField(label="Quantity (units)", min_value=1)
    field_order = ["quantity", "reason"]


class AdjustForm(QuantityReasonForm):
    direction = forms.ChoiceField(choices=[("add", "Add to stock"), ("remove", "Remove from stock")],
                                  label="Direction", initial="remove")
    field_order = ["direction", "quantity", "reason"]
