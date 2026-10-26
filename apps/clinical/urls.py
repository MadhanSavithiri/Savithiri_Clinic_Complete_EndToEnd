from django.urls import path

from . import views

app_name = "clinical"

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("start/<int:token_pk>/", views.start, name="start"),
    path("patient/<int:pk>/", views.patient_record, name="record"),
    path("encounter/<int:pk>/", views.encounter, name="encounter"),
    path("encounter/<int:pk>/notes/", views.notes_save, name="notes_save"),
    path("encounter/<int:pk>/vitals/", views.vitals_add, name="vitals_add"),
    path("encounter/<int:pk>/vitals/<int:vital_pk>/void/", views.vitals_void, name="vitals_void"),
    path("encounter/<int:pk>/diagnosis/", views.diagnosis_add, name="diagnosis_add"),
    path("encounter/<int:pk>/diagnosis/<int:dx_pk>/void/", views.diagnosis_void, name="diagnosis_void"),
    path("encounter/<int:pk>/allergies/add/", views.allergy_add, name="allergy_add"),
    path("encounter/<int:pk>/allergies/none/", views.allergy_nkda, name="allergy_nkda"),
    path("encounter/<int:pk>/history/add/", views.history_add, name="history_add"),
    path("encounter/<int:pk>/current-medicines/add/", views.medicine_add, name="medicine_add"),
    path("encounter/<int:pk>/record/<str:kind>/<int:record_pk>/status/", views.record_status, name="record_status"),
    path("encounter/<int:pk>/referral/", views.referral_add, name="referral_add"),
    path("encounter/<int:pk>/note/", views.note_add, name="note_add"),
    path("encounter/<int:pk>/complete/", views.complete, name="complete"),
    path("encounter/<int:pk>/reopen/", views.reopen, name="reopen"),
    path("encounter/<int:pk>/rx/item/add/", views.rx_item_add, name="rx_item_add"),
    path("encounter/<int:pk>/rx/item/<int:item_pk>/edit/", views.rx_item_edit, name="rx_item_edit"),
    path("encounter/<int:pk>/rx/item/<int:item_pk>/remove/", views.rx_item_remove, name="rx_item_remove"),
    path("rx/<int:rx_pk>/discard/", views.rx_discard, name="rx_discard"),
    path("rx/<int:rx_pk>/finalize/", views.rx_finalize, name="rx_finalize"),
    path("rx/<int:rx_pk>/print/", views.rx_print, name="rx_print"),
    path("rx/<int:rx_pk>/correct/", views.rx_correct, name="rx_correct"),
    path("rx/<int:rx_pk>/cancel/", views.rx_cancel, name="rx_cancel"),
]
