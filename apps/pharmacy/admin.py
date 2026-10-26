from django.contrib import admin

from apps.patients.admin import ReadOnlyAdmin

from . import models


@admin.register(models.Medicine)
class MedicineAdmin(ReadOnlyAdmin):
    list_display = ("brand_name", "strength", "dosage_form", "generic_name", "is_active")
    search_fields = ("brand_name", "generic_name")


@admin.register(models.MedicineBatch)
class MedicineBatchAdmin(ReadOnlyAdmin):
    list_display = ("medicine", "batch_number", "expiry_date", "quantity_on_hand", "is_recalled")
    list_filter = ("is_recalled",)


for _m in (models.Supplier, models.PurchaseInvoice, models.PurchaseInvoiceItem, models.InventoryTransaction):
    admin.site.register(_m, type(f"{_m.__name__}Admin", (ReadOnlyAdmin,), {}))
