from django.contrib import admin

from apps.patients.admin import ReadOnlyAdmin

from . import models

for _m in (models.Shift, models.Sale, models.SaleLine, models.Payment):
    admin.site.register(_m, type(f"{_m.__name__}Admin", (ReadOnlyAdmin,), {}))
