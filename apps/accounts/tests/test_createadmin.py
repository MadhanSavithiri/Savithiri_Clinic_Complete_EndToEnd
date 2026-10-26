from io import StringIO
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase

from apps.core.models import AuditEvent

GOOD = "Cl1nic-Strong-Pass!"


def run(username="boss", passwords=(GOOD, GOOD)):
    out = StringIO()
    with mock.patch("getpass.getpass", side_effect=list(passwords)):
        call_command("createadmin", username=username, stdout=out)
    return out.getvalue()


class CreateAdminTests(TestCase):
    def test_creates_admin_with_hashed_password_and_audit(self):
        run()
        user = get_user_model().objects.get(username="boss")
        self.assertEqual(user.role, "admin")
        self.assertTrue(user.is_staff and user.is_superuser and user.is_active)
        self.assertNotEqual(user.password, GOOD)
        self.assertTrue(user.check_password(GOOD))
        self.assertTrue(AuditEvent.objects.filter(action="user.create_admin").exists())

    def test_no_default_accounts_exist(self):
        self.assertEqual(get_user_model().objects.count(), 0)

    def test_mismatched_passwords_rejected(self):
        with self.assertRaises(CommandError):
            run(passwords=(GOOD, "different-Pass-9"))
        self.assertEqual(get_user_model().objects.count(), 0)

    def test_weak_passwords_rejected(self):
        for weak in ("short", "1234567890", "password123"):
            with self.assertRaises(CommandError):
                run(passwords=(weak, weak))
        self.assertEqual(get_user_model().objects.count(), 0)

    def test_duplicate_username_rejected(self):
        run()
        with self.assertRaises(CommandError):
            run(username="BOSS")
