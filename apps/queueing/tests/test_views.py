from datetime import timedelta

from django.contrib.messages import get_messages
from django.contrib.sessions.models import Session
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from apps.accounts.tests.helpers import make_user
from apps.patients.tests.helpers import make_patient
from apps.queueing import services
from apps.queueing.models import Appointment, QueueToken


def msgs(response):
    return [str(m) for m in get_messages(response.wsgi_request)]


class ViewBase(TestCase):
    def setUp(self):
        self.reception = make_user("rita", "reception")
        self.doctor = make_user("doc", "doctor")
        self.pharmacy = make_user("pharm", "pharmacy")
        self.admin = make_user("boss", "admin")
        self.p1 = make_patient(self.reception, name="Anbu Selvan", phone="9000000001")
        self.p2 = make_patient(self.reception, name="Meena Devi", phone="9000000002", sex="F")

    def login(self, user):
        self.client.force_login(user)


class WalkInViewTests(ViewBase):
    def test_walk_in_creates_appointment_and_token(self):
        self.login(self.reception)
        r = self.client.post(reverse("queueing:walk_in", args=[self.p1.pk]))
        self.assertRedirects(r, reverse("queueing:queue"), fetch_redirect_response=False)
        token = QueueToken.objects.get()
        self.assertEqual(token.patient, self.p1)
        self.assertEqual(token.appointment.kind, "WALK_IN")
        self.assertIn("Print token slip", msgs(r)[-1])

    def test_second_click_gives_a_message_not_a_second_token(self):
        self.login(self.reception)
        self.client.post(reverse("queueing:walk_in", args=[self.p1.pk]))
        r = self.client.post(reverse("queueing:walk_in", args=[self.p1.pk]))
        self.assertIn("already in today's queue", msgs(r)[-1])
        self.assertEqual(QueueToken.objects.count(), 1)
        self.assertEqual(Appointment.objects.count(), 1)

    def test_other_roles_cannot_add_and_get_is_not_allowed(self):
        for user in (self.doctor, self.pharmacy, self.admin):
            self.login(user)
            self.assertEqual(self.client.post(reverse("queueing:walk_in", args=[self.p1.pk])).status_code, 403)
        self.login(self.reception)
        self.assertEqual(self.client.get(reverse("queueing:walk_in", args=[self.p1.pk])).status_code, 405)
        self.assertEqual(QueueToken.objects.count(), 0)


class QueueBoardTests(ViewBase):
    def setUp(self):
        super().setUp()
        self.t1 = services.add_to_queue(actor=self.reception, request=None, patient=self.p1)
        self.t2 = services.add_to_queue(actor=self.reception, request=None, patient=self.p2)

    def test_reception_sees_board_with_counts_and_cancel_but_no_call(self):
        self.login(self.reception)
        r = self.client.get(reverse("queueing:queue"))
        self.assertContains(r, "Anbu Selvan")
        self.assertContains(r, "Meena Devi")
        self.assertContains(r, 'data-live-url="/queue/partial/"')
        self.assertContains(r, "Cancel")
        self.assertNotContains(r, "Start consultation")
        self.assertEqual(r.context["counts"]["WAITING"], 2)

    def test_doctor_sees_call_buttons(self):
        self.login(self.doctor)
        self.assertContains(self.client.get(reverse("queueing:queue_partial"), HTTP_X_LIVE_POLL="1"), "Start consultation")

    def test_partial_reflects_changes_from_another_workstation(self):
        self.login(self.reception)
        self.assertContains(self.client.get(reverse("queueing:queue_partial")), "Waiting")
        services.change_token_status(actor=self.doctor, request=None, token_id=self.t1.pk, action="call")
        r = self.client.get(reverse("queueing:queue_partial"))
        self.assertEqual(r.context["counts"]["IN_CONSULTATION"], 1)
        self.assertContains(r, "In consultation")

    def test_pharmacy_refused_and_anonymous_poll_gets_401(self):
        self.login(self.pharmacy)
        self.assertEqual(self.client.get(reverse("queueing:queue")).status_code, 403)
        self.assertEqual(self.client.get(reverse("queueing:queue_partial")).status_code, 403)
        self.client.logout()
        self.assertEqual(self.client.get(reverse("queueing:queue_partial")).status_code, 401)

    def test_doctor_calls_and_completes_through_the_screen(self):
        self.login(self.doctor)
        self.client.post(reverse("queueing:token_call", args=[self.t1.pk]))
        self.assertEqual(QueueToken.objects.get(pk=self.t1.pk).status, "IN_CONSULTATION")
        self.client.post(reverse("queueing:token_complete", args=[self.t1.pk]))
        self.assertEqual(QueueToken.objects.get(pk=self.t1.pk).status, "COMPLETED")

    def test_reception_cannot_call_through_the_screen(self):
        self.login(self.reception)
        self.assertEqual(self.client.post(reverse("queueing:token_call", args=[self.t1.pk])).status_code, 403)
        self.assertEqual(QueueToken.objects.get(pk=self.t1.pk).status, "WAITING")

    def test_stale_click_shows_message(self):
        self.login(self.doctor)
        self.client.post(reverse("queueing:token_call", args=[self.t1.pk]))
        r = self.client.post(reverse("queueing:token_call", args=[self.t1.pk]))
        self.assertIn("Someone else changed it", msgs(r)[-1])

    def test_cancel_needs_a_reason_via_the_screen(self):
        self.login(self.reception)
        url = reverse("queueing:token_cancel", args=[self.t2.pk])
        self.assertContains(self.client.get(url), "Cancel token 2")
        self.assertEqual(self.client.post(url, {"reason": ""}).status_code, 200)
        self.assertEqual(QueueToken.objects.get(pk=self.t2.pk).status, "WAITING")
        self.client.post(url, {"reason": "Patient left"})
        self.assertEqual(QueueToken.objects.get(pk=self.t2.pk).status, "CANCELLED")

    def test_token_slip(self):
        self.login(self.reception)
        r = self.client.get(reverse("queueing:token_print", args=[self.t1.pk]))
        self.assertContains(r, "Anbu Selvan")
        self.assertContains(r, "Savithiri Clinic")
        self.login(self.pharmacy)
        self.assertEqual(self.client.get(reverse("queueing:token_print", args=[self.t1.pk])).status_code, 403)


class AppointmentViewTests(ViewBase):
    def post_booking(self, day, patient=None):
        return self.client.post(reverse("queueing:appointment_create", args=[(patient or self.p1).pk]),
                                {"date": day.isoformat(), "time": "10:30", "reason": "Review"})

    def test_book_list_check_in_and_cancel(self):
        self.login(self.reception)
        today = timezone.localdate()
        tomorrow = today + timedelta(days=1)
        self.post_booking(tomorrow)
        self.post_booking(today, self.p2)
        self.assertEqual(Appointment.objects.count(), 2)
        r = self.client.get(reverse("queueing:appointment_list"), {"date": today.isoformat()})
        self.assertContains(r, "Meena Devi")
        self.assertContains(r, "Check in")

        today_appt = Appointment.objects.get(scheduled_date=today)
        self.client.post(reverse("queueing:appointment_checkin", args=[today_appt.pk]))
        self.assertEqual(QueueToken.objects.get().patient, self.p2)

        future = Appointment.objects.get(scheduled_date=tomorrow)
        r = self.client.post(reverse("queueing:appointment_checkin", args=[future.pk]))
        self.assertIn("not today", msgs(r)[-1])
        self.assertEqual(QueueToken.objects.count(), 1)

        self.client.post(reverse("queueing:appointment_cancel", args=[future.pk]), {"reason": "Rescheduled"})
        future.refresh_from_db()
        self.assertEqual((future.status, future.cancel_reason), ("CANCELLED", "Rescheduled"))

    def test_past_date_rejected_by_form(self):
        self.login(self.reception)
        r = self.post_booking(timezone.localdate() - timedelta(days=1))
        self.assertContains(r, "Choose today or a future date")
        self.assertEqual(Appointment.objects.count(), 0)

    def test_doctor_can_view_list_but_not_book_or_check_in(self):
        self.login(self.doctor)
        self.assertEqual(self.client.get(reverse("queueing:appointment_list")).status_code, 200)
        self.assertEqual(self.client.get(reverse("queueing:appointment_create", args=[self.p1.pk])).status_code, 403)
        appt = services.book_appointment(actor=self.reception, request=None, patient=self.p1, date=timezone.localdate())
        self.assertEqual(self.client.post(reverse("queueing:appointment_checkin", args=[appt.pk])).status_code, 403)

    def test_pharmacy_cannot_see_appointments(self):
        self.login(self.pharmacy)
        self.assertEqual(self.client.get(reverse("queueing:appointment_list")).status_code, 403)


class LiveRefreshSessionTests(ViewBase):
    def test_background_polls_do_not_extend_idle_timeout_but_real_requests_do(self):
        self.login(self.reception)
        key = self.client.session.session_key
        soon = timezone.now() + timedelta(seconds=60)
        Session.objects.filter(session_key=key).update(expire_date=soon)

        self.client.get(reverse("queueing:queue_partial"), HTTP_X_LIVE_POLL="1")
        self.assertEqual(Session.objects.get(session_key=key).expire_date, soon)

        self.client.get(reverse("queueing:queue"))
        self.assertGreater(Session.objects.get(session_key=key).expire_date, soon + timedelta(minutes=10))
