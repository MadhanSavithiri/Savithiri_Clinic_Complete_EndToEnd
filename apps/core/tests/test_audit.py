import unittest

from django.contrib.auth import get_user_model
from django.db import DatabaseError, connection, transaction
from django.test import RequestFactory, TestCase

from apps.core.audit import log_event
from apps.core.models import AuditEvent, AuditImmutableError


class AuditTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("doc1", password="x" * 14, role="doctor")

    def test_log_event_records_user_ip_and_object(self):
        request = RequestFactory().get("/", REMOTE_ADDR="192.168.1.20")
        request.user = self.user
        event = log_event(request=request, action="test.action", obj=self.user, changes={"a": 1})
        event.refresh_from_db()
        self.assertEqual(event.username, "doc1")
        self.assertEqual(event.ip_address, "192.168.1.20")
        self.assertEqual(event.object_id, str(self.user.pk))
        self.assertEqual(event.changes, {"a": 1})

    def test_cannot_modify_or_delete_via_orm(self):
        event = log_event(user=self.user, action="x")
        event.summary = "tampered"
        with self.assertRaises(AuditImmutableError):
            event.save()
        with self.assertRaises(AuditImmutableError):
            event.delete()
        with self.assertRaises(AuditImmutableError):
            AuditEvent.objects.all().update(summary="tampered")
        with self.assertRaises(AuditImmutableError):
            AuditEvent.objects.all().delete()

    @unittest.skipUnless(connection.vendor == "postgresql", "Trigger exists only on PostgreSQL")
    def test_database_trigger_blocks_raw_sql(self):
        log_event(user=self.user, action="x")
        with self.assertRaises(DatabaseError):
            with transaction.atomic():
                with connection.cursor() as cur:
                    cur.execute("UPDATE core_auditevent SET summary = 'tampered'")
        with self.assertRaises(DatabaseError):
            with transaction.atomic():
                with connection.cursor() as cur:
                    cur.execute("DELETE FROM core_auditevent")
        self.assertEqual(AuditEvent.objects.count(), 1)
