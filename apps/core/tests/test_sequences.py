import threading
import unittest

from django.db import connection, connections
from django.test import TestCase, TransactionTestCase

from apps.core.models import DocType, DocumentSequence
from apps.core.sequences import next_number


class SequenceFormatTests(TestCase):
    def test_numbers_increase_by_one(self):
        first = next_number(DocType.PRESCRIPTION, period="2026")
        second = next_number(DocType.PRESCRIPTION, period="2026")
        self.assertEqual((first.value, second.value), (1, 2))
        self.assertEqual(first.formatted, "RX-2026-000001")
        self.assertEqual(second.formatted, "RX-2026-000002")

    def test_periods_and_types_count_independently(self):
        a = next_number(DocType.PRESCRIPTION, period="2026")
        b = next_number(DocType.PRESCRIPTION, period="2027")
        c = next_number(DocType.PATIENT)
        self.assertEqual(a.value, 1)
        self.assertEqual(b.value, 1)
        self.assertEqual(c.formatted, "P-000001")

    def test_token_format_has_three_digits(self):
        self.assertEqual(next_number(DocType.TOKEN, period="20261015").formatted, "T-20261015-001")

    def test_rollback_does_not_burn_a_number(self):
        from django.db import transaction

        class Boom(Exception):
            pass

        with self.assertRaises(Boom):
            with transaction.atomic():
                next_number(DocType.PATIENT)
                raise Boom()
        self.assertEqual(next_number(DocType.PATIENT).value, 1)

    def test_sequence_row_stored(self):
        next_number(DocType.PATIENT)
        self.assertEqual(DocumentSequence.objects.get(doc_type="PATIENT").next_value, 2)


@unittest.skipUnless(connection.vendor == "postgresql", "Concurrency test needs PostgreSQL")
class ConcurrentNumberingTests(TransactionTestCase):
    def test_20_simultaneous_requests_get_unique_consecutive_numbers(self):
        results, errors = [], []
        barrier = threading.Barrier(20)

        def worker():
            try:
                barrier.wait()
                results.append(next_number(DocType.PRESCRIPTION, period="2026").value)
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)
            finally:
                connections.close_all()

        threads = [threading.Thread(target=worker) for _ in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [])
        self.assertEqual(sorted(results), list(range(1, 21)))
