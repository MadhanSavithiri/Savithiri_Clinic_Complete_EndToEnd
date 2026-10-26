import threading
import unittest
from datetime import date, timedelta

from django.core.exceptions import PermissionDenied
from django.db import connection, connections
from django.test import TestCase, TransactionTestCase

from apps.accounts.tests.helpers import make_user
from apps.core.models import AuditEvent
from apps.patients.tests.helpers import make_patient
from apps.queueing import services
from apps.queueing.models import Appointment, QueueToken

D = date(2026, 10, 19)


def add(actor, patient, today=D):
    return services.add_to_queue(actor=actor, request=None, patient=patient, today=today)


class BaseCase(TestCase):
    def setUp(self):
        self.reception = make_user("rita", "reception")
        self.doctor = make_user("doc", "doctor")
        self.pharmacy = make_user("pharm", "pharmacy")
        self.p1 = make_patient(self.reception, name="Anbu Selvan", phone="9000000001")
        self.p2 = make_patient(self.reception, name="Meena Devi", phone="9000000002", sex="F")


class TokenTests(BaseCase):
    def test_numbers_are_sequential_and_reset_each_day(self):
        t1, t2 = add(self.reception, self.p1), add(self.reception, self.p2)
        self.assertEqual((t1.token_number, t2.token_number), (1, 2))
        self.assertEqual(t1.code, "T-20261019-001")
        next_day = add(self.reception, self.p1, today=D + timedelta(days=1))
        self.assertEqual(next_day.token_number, 1)

    def test_patient_cannot_have_two_open_tokens_in_a_day(self):
        add(self.reception, self.p1)
        with self.assertRaises(services.QueueError) as ctx:
            add(self.reception, self.p1)
        self.assertIn("already in today's queue", str(ctx.exception))
        self.assertEqual(QueueToken.objects.count(), 1)

    def test_failed_add_does_not_burn_a_number(self):
        add(self.reception, self.p1)
        with self.assertRaises(services.QueueError):
            add(self.reception, self.p1)
        self.assertEqual(add(self.reception, self.p2).token_number, 2)

    def test_after_completion_the_patient_can_return_with_a_new_token(self):
        t = add(self.reception, self.p1)
        for action in ("call", "complete"):
            services.change_token_status(actor=self.doctor, request=None, token_id=t.pk, action=action)
        self.assertEqual(add(self.reception, self.p1).token_number, 2)

    def test_cancelled_token_frees_the_patient(self):
        t = add(self.reception, self.p1)
        services.change_token_status(actor=self.reception, request=None, token_id=t.pk, action="cancel", reason="Left")
        self.assertEqual(add(self.reception, self.p1).token_number, 2)

    def test_only_reception_can_add(self):
        for user in (self.doctor, self.pharmacy, make_user("boss", "admin")):
            with self.assertRaises(PermissionDenied):
                add(user, self.p1)
        self.assertEqual(QueueToken.objects.count(), 0)

    def test_merged_patient_cannot_be_queued(self):
        from apps.patients.models import Patient
        Patient.objects.filter(pk=self.p1.pk).update(is_active=False, merged_into=self.p2)
        self.p1.refresh_from_db()
        with self.assertRaises(services.QueueError):
            add(self.reception, self.p1)


class StatusTests(BaseCase):
    def setUp(self):
        super().setUp()
        self.token = add(self.reception, self.p1)

    def act(self, user, action, reason=""):
        return services.change_token_status(actor=user, request=None, token_id=self.token.pk, action=action, reason=reason)

    def test_doctor_flow_call_return_call_complete(self):
        self.assertEqual(self.act(self.doctor, "call").status, "IN_CONSULTATION")
        self.assertIsNotNone(QueueToken.objects.get().called_at)
        self.assertEqual(self.act(self.doctor, "return").status, "WAITING")
        self.assertIsNone(QueueToken.objects.get().called_at)
        self.act(self.doctor, "call")
        t = self.act(self.doctor, "complete")
        self.assertEqual(t.status, "COMPLETED")
        self.assertIsNotNone(t.completed_at)

    def test_reception_cannot_call_or_complete(self):
        for action in ("call", "return", "complete"):
            with self.assertRaises(PermissionDenied):
                self.act(self.reception, action)
        self.assertEqual(QueueToken.objects.get().status, "WAITING")

    def test_doctor_and_reception_can_cancel_waiting_with_reason(self):
        with self.assertRaises(services.QueueError):
            self.act(self.reception, "cancel", reason="  ")
        t = self.act(self.reception, "cancel", reason="Patient left")
        self.assertEqual((t.status, t.cancel_reason), ("CANCELLED", "Patient left"))
        self.assertIsNotNone(t.cancelled_at)

    def test_pharmacy_cannot_change_anything(self):
        for action in ("call", "cancel"):
            with self.assertRaises(PermissionDenied):
                self.act(self.pharmacy, action, reason="x")

    def test_invalid_and_stale_transitions_are_refused(self):
        with self.assertRaises(services.QueueError):
            self.act(self.doctor, "complete")          # still waiting
        self.act(self.doctor, "call")
        with self.assertRaises(services.QueueError):
            self.act(self.doctor, "call")              # second click / other workstation
        with self.assertRaises(services.QueueError):
            self.act(self.reception, "cancel", reason="x")  # already inside
        self.act(self.doctor, "complete")
        with self.assertRaises(services.QueueError):
            self.act(self.doctor, "return")

    def test_every_change_is_audited_with_old_and_new_status(self):
        self.act(self.doctor, "call")
        ev = AuditEvent.objects.get(action="queue.call")
        self.assertEqual(ev.username, "doc")
        self.assertEqual(ev.changes["status"], ["WAITING", "IN_CONSULTATION"])

    def test_cancelling_a_walk_in_token_cancels_its_appointment(self):
        t = services.register_walk_in(actor=self.reception, request=None, patient=self.p2, today=D)
        services.change_token_status(actor=self.reception, request=None, token_id=t.pk, action="cancel", reason="Gone")
        self.assertEqual(Appointment.objects.get(pk=t.appointment_id).status, "CANCELLED")


class AppointmentServiceTests(BaseCase):
    def book(self, day, patient=None):
        return services.book_appointment(actor=self.reception, request=None, patient=patient or self.p1, date=day, today=D)

    def test_walk_in_creates_checked_in_appointment_and_token(self):
        t = services.register_walk_in(actor=self.reception, request=None, patient=self.p1, today=D)
        a = Appointment.objects.get()
        self.assertEqual((a.kind, a.status, a.scheduled_date), ("WALK_IN", "CHECKED_IN", D))
        self.assertEqual(t.appointment, a)

    def test_failed_walk_in_leaves_no_orphan_appointment(self):
        add(self.reception, self.p1)
        with self.assertRaises(services.QueueError):
            services.register_walk_in(actor=self.reception, request=None, patient=self.p1, today=D)
        self.assertEqual(Appointment.objects.count(), 0)

    def test_cannot_book_in_the_past(self):
        with self.assertRaises(services.QueueError):
            self.book(D - timedelta(days=1))

    def test_check_in_only_on_the_day(self):
        future = self.book(D + timedelta(days=2))
        with self.assertRaises(services.QueueError):
            services.check_in_appointment(actor=self.reception, request=None, appointment_id=future.pk, today=D)
        today_appt = self.book(D, patient=self.p2)
        token = services.check_in_appointment(actor=self.reception, request=None, appointment_id=today_appt.pk, today=D)
        today_appt.refresh_from_db()
        self.assertEqual(today_appt.status, "CHECKED_IN")
        self.assertEqual(token.appointment, today_appt)
        with self.assertRaises(services.QueueError):  # cannot check in twice
            services.check_in_appointment(actor=self.reception, request=None, appointment_id=today_appt.pk, today=D)
        self.assertEqual(QueueToken.objects.count(), 1)

    def test_check_in_fails_cleanly_if_patient_already_queued(self):
        add(self.reception, self.p1)
        appt = self.book(D)
        with self.assertRaises(services.QueueError):
            services.check_in_appointment(actor=self.reception, request=None, appointment_id=appt.pk, today=D)
        appt.refresh_from_db()
        self.assertEqual(appt.status, "SCHEDULED")

    def test_cancel_needs_reason_and_scheduled_status(self):
        appt = self.book(D + timedelta(days=1))
        with self.assertRaises(services.QueueError):
            services.cancel_appointment(actor=self.reception, request=None, appointment_id=appt.pk, reason="")
        services.cancel_appointment(actor=self.reception, request=None, appointment_id=appt.pk, reason="Called to cancel")
        with self.assertRaises(services.QueueError):
            services.cancel_appointment(actor=self.reception, request=None, appointment_id=appt.pk, reason="again")


@unittest.skipUnless(connection.vendor == "postgresql", "Concurrency tests need PostgreSQL")
class ConcurrentQueueTests(TransactionTestCase):
    def setUp(self):
        self.reception = make_user("rita", "reception")

    def run_threads(self, targets):
        results, errors = [], []
        barrier = threading.Barrier(len(targets))

        def worker(fn):
            try:
                barrier.wait()
                results.append(fn())
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)
            finally:
                connections.close_all()

        threads = [threading.Thread(target=worker, args=(fn,)) for fn in targets]
        [t.start() for t in threads]
        [t.join() for t in threads]
        return results, errors

    def test_15_workstation_clicks_get_unique_consecutive_tokens(self):
        patients = [make_patient(self.reception, name=f"Patient Number{i}", phone=f"90000{i:05d}") for i in range(15)]
        targets = [lambda p=p: add(self.reception, p).token_number for p in patients]
        results, errors = self.run_threads(targets)
        self.assertEqual(errors, [])
        self.assertEqual(sorted(results), list(range(1, 16)))
        self.assertEqual(QueueToken.objects.count(), 15)

    def test_same_patient_added_six_times_at_once_gets_exactly_one_token(self):
        patient = make_patient(self.reception)
        results, errors = self.run_threads([lambda: add(self.reception, patient) for _ in range(6)])
        self.assertEqual(len(results), 1)
        self.assertEqual(len(errors), 5)
        self.assertTrue(all(isinstance(e, services.QueueError) for e in errors))
        self.assertEqual(QueueToken.objects.count(), 1)
        # no number was wasted by the failed attempts
        other = make_patient(self.reception, name="Next Person", phone="9111111111")
        self.assertEqual(add(self.reception, other).token_number, 2)

    def test_double_clicked_call_succeeds_once(self):
        doctor = make_user("doc", "doctor")
        token = add(self.reception, make_patient(self.reception))
        call = lambda: services.change_token_status(actor=doctor, request=None, token_id=token.pk, action="call")  # noqa: E731
        results, errors = self.run_threads([call, call, call])
        self.assertEqual(len(results), 1)
        self.assertEqual(len(errors), 2)
