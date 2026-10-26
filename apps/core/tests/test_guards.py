import unittest

from django.db import DatabaseError, connection, transaction
from django.test import TestCase

from apps.core.db_guards import APPEND_ONLY_TABLES


@unittest.skipUnless(connection.vendor == "postgresql", "Triggers exist only on PostgreSQL")
class AppendOnlyTriggerInstalledTests(TestCase):
    def test_every_protected_table_has_its_trigger(self):
        with connection.cursor() as cur:
            cur.execute("SELECT event_object_table FROM information_schema.triggers WHERE trigger_name LIKE %s",
                        ["%_append_only"])
            protected = {row[0] for row in cur.fetchall()}
        self.assertEqual(protected, set(APPEND_ONLY_TABLES))
