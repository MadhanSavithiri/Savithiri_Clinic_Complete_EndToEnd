from django.urls import path

from . import views

app_name = "queueing"

urlpatterns = [
    path("queue/", views.queue_page, name="queue"),
    path("queue/partial/", views.queue_partial, name="queue_partial"),
    path("queue/walk-in/<int:patient_pk>/", views.walk_in, name="walk_in"),
    path("queue/token/<int:pk>/call/", views.token_call, name="token_call"),
    path("queue/token/<int:pk>/return/", views.token_return, name="token_return"),
    path("queue/token/<int:pk>/complete/", views.token_complete, name="token_complete"),
    path("queue/token/<int:pk>/cancel/", views.token_cancel, name="token_cancel"),
    path("queue/token/<int:pk>/print/", views.token_print, name="token_print"),
    path("appointments/", views.appointment_list, name="appointment_list"),
    path("appointments/new/<int:patient_pk>/", views.appointment_create, name="appointment_create"),
    path("appointments/<int:pk>/check-in/", views.appointment_checkin, name="appointment_checkin"),
    path("appointments/<int:pk>/cancel/", views.appointment_cancel, name="appointment_cancel"),
]
