from django.conf import settings
from django.contrib import admin
from django.urls import include, path

from apps.core.views import health, home

admin.site.site_header = "Savithiri Clinic - Administration"
admin.site.site_title = "Savithiri Clinic"
admin.site.index_title = "Clinic administration"

urlpatterns = [
    path("health/", health, name="health"),
    path(f"{settings.ADMIN_URL}/", admin.site.urls),
    path("", include("apps.accounts.urls")),
    path("patients/", include("apps.patients.urls")),
    path("", include("apps.queueing.urls")),
    path("consult/", include("apps.clinical.urls")),
    path("medicines/", include("apps.pharmacy.urls")),
    path("counter/", include("apps.sales.urls")),
    path("", home, name="home"),
]
