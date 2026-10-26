from django.urls import path

from . import views

app_name = "patients"

urlpatterns = [
    path("", views.patient_list, name="list"),
    path("search/", views.patient_search_partial, name="search_partial"),
    path("new/", views.patient_create, name="create"),
    path("<int:pk>/", views.patient_detail, name="detail"),
    path("<int:pk>/edit/", views.patient_edit, name="edit"),
    path("<int:pk>/identifiers/add/", views.identifier_add, name="identifier_add"),
    path("<int:pk>/merge/", views.patient_merge, name="merge"),
    path("<int:pk>/merge/<int:source_pk>/", views.patient_merge_confirm, name="merge_confirm"),
]
