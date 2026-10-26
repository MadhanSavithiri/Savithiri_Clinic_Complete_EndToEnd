from datetime import date

from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import formats


class HealthTests(TestCase):
    def test_health_ok_without_login_and_without_data_leak(self):
        response = self.client.get(reverse("health"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok"})


class LocaleTests(TestCase):
    def test_india_settings(self):
        self.assertEqual(settings.TIME_ZONE, "Asia/Kolkata")
        self.assertEqual(settings.CURRENCY_CODE, "INR")

    def test_indian_date_format(self):
        self.assertEqual(formats.date_format(date(2026, 10, 15), "SHORT_DATE_FORMAT"), "15/10/2026")


class AdminProtectionTests(TestCase):
    def setUp(self):
        self.admin = get_user_model().objects.create_superuser("boss", password="S3cure-pass-123")
        self.client.force_login(self.admin)

    def test_audit_log_is_read_only_even_for_superuser(self):
        self.assertEqual(self.client.get(reverse("admin:core_auditevent_changelist")).status_code, 200)
        self.assertEqual(self.client.get(reverse("admin:core_auditevent_add")).status_code, 403)

    def test_users_cannot_be_deleted_in_admin(self):
        url = reverse("admin:accounts_user_delete", args=[self.admin.pk])
        self.assertEqual(self.client.get(url).status_code, 403)

    def test_anonymous_cannot_open_admin(self):
        self.client.logout()
        response = self.client.get(reverse("admin:index"))
        self.assertEqual(response.status_code, 302)
