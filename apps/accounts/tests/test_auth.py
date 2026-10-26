from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from apps.core.models import AuditEvent

from .helpers import PW, make_user

User = get_user_model()


def login(client, username, password):
    return client.post(reverse("accounts:login"), {"username": username, "password": password})


class LoginTests(TestCase):
    def setUp(self):
        self.user = make_user("rita", "reception")

    def test_login_success_redirects_home_and_is_audited(self):
        r = login(self.client, "rita", PW)
        self.assertRedirects(r, reverse("home"), fetch_redirect_response=False)
        self.assertIn("_auth_user_id", self.client.session)
        self.assertTrue(AuditEvent.objects.filter(action="auth.login", username="rita").exists())

    def test_wrong_password_is_generic_and_counted(self):
        r = login(self.client, "rita", "wrong-password-1")
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "Invalid username or password.")
        self.assertNotIn("_auth_user_id", self.client.session)
        self.user.refresh_from_db()
        self.assertEqual(self.user.failed_login_count, 1)
        self.assertTrue(AuditEvent.objects.filter(action="auth.login_failed", username="rita").exists())

    def test_unknown_user_same_message_and_typed_text_not_stored(self):
        r = login(self.client, "nobody-secret-text", "whatever-123")
        self.assertContains(r, "Invalid username or password.")
        self.assertTrue(AuditEvent.objects.filter(action="auth.login_failed", username="").exists())
        self.assertFalse(AuditEvent.objects.filter(summary__icontains="nobody-secret-text").exists())
        self.assertFalse(AuditEvent.objects.filter(username__icontains="nobody").exists())

    def test_success_resets_failure_counter(self):
        login(self.client, "rita", "bad-password-1")
        login(self.client, "rita", PW)
        self.user.refresh_from_db()
        self.assertEqual(self.user.failed_login_count, 0)

    def test_inactive_user_cannot_login(self):
        self.user.is_active = False
        self.user.save()
        r = login(self.client, "rita", PW)
        self.assertContains(r, "Invalid username or password.")
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_anonymous_home_redirects_to_login(self):
        r = self.client.get(reverse("home"))
        self.assertRedirects(r, f"{reverse('accounts:login')}?next=/", fetch_redirect_response=False)


class LockoutTests(TestCase):
    def setUp(self):
        self.user = make_user("rita", "reception")

    def lock_it(self):
        for _ in range(5):
            login(self.client, "rita", "bad-password-1")
        self.user.refresh_from_db()

    def test_five_failures_lock_account_even_for_correct_password(self):
        self.lock_it()
        self.assertTrue(self.user.is_locked())
        self.assertTrue(AuditEvent.objects.filter(action="auth.account_locked", username="rita").exists())
        r = login(self.client, "rita", PW)
        self.assertEqual(r.status_code, 200)
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_lock_expires(self):
        self.lock_it()
        User.objects.filter(pk=self.user.pk).update(locked_until=timezone.now() - timedelta(seconds=1))
        r = login(self.client, "rita", PW)
        self.assertRedirects(r, reverse("home"), fetch_redirect_response=False)

    def test_lock_does_not_kick_out_someone_already_signed_in(self):
        login(self.client, "rita", PW)
        User.objects.filter(pk=self.user.pk).update(locked_until=timezone.now() + timedelta(minutes=10))
        self.assertEqual(self.client.get(reverse("home")).status_code, 200)

    def test_attempts_while_locked_do_not_extend_lock_or_flood_audit(self):
        self.lock_it()
        until = self.user.locked_until
        before = AuditEvent.objects.count()
        for _ in range(3):
            login(self.client, "rita", "bad-password-1")
        self.user.refresh_from_db()
        self.assertEqual(self.user.locked_until, until)
        self.assertEqual(AuditEvent.objects.count(), before)


class LogoutAndSessionTests(TestCase):
    def setUp(self):
        self.user = make_user("rita", "reception")
        login(self.client, "rita", PW)

    def test_logout_requires_post(self):
        self.assertEqual(self.client.get(reverse("accounts:logout")).status_code, 405)
        self.assertIn("_auth_user_id", self.client.session)

    def test_logout_post_ends_session_and_is_audited(self):
        r = self.client.post(reverse("accounts:logout"))
        self.assertEqual(r.status_code, 302)
        self.assertNotIn("_auth_user_id", self.client.session)
        self.assertTrue(AuditEvent.objects.filter(action="auth.logout", username="rita").exists())

    def test_deactivating_user_ends_their_open_session(self):
        User.objects.filter(pk=self.user.pk).update(is_active=False)
        r = self.client.get(reverse("home"))
        self.assertEqual(r.status_code, 302)
        self.assertIn(reverse("accounts:login"), r["Location"])


class ForcedPasswordChangeTests(TestCase):
    def setUp(self):
        self.user = make_user("newbie", "doctor", must_change_password=True)
        login(self.client, "newbie", PW)

    def test_everything_redirects_to_password_change(self):
        for url in (reverse("home"), "/staff/", "/admin/"):
            r = self.client.get(url)
            self.assertRedirects(r, reverse("accounts:password_change"), fetch_redirect_response=False)

    def test_health_logout_and_change_page_still_work(self):
        self.assertEqual(self.client.get(reverse("health")).status_code, 200)
        self.assertEqual(self.client.get(reverse("accounts:password_change")).status_code, 200)
        self.assertEqual(self.client.post(reverse("accounts:logout")).status_code, 302)

    def change(self, old=PW, new="A-Brand-New-Pass-77"):
        return self.client.post(reverse("accounts:password_change"),
                                {"old_password": old, "new_password1": new, "new_password2": new})

    def test_successful_change_clears_flag_keeps_session_and_audits(self):
        r = self.change()
        self.assertRedirects(r, reverse("home"), fetch_redirect_response=False)
        self.user.refresh_from_db()
        self.assertFalse(self.user.must_change_password)
        self.assertTrue(self.user.check_password("A-Brand-New-Pass-77"))
        self.assertEqual(self.client.get(reverse("home")).status_code, 200)
        self.assertTrue(AuditEvent.objects.filter(action="auth.password_changed", username="newbie").exists())
        self.assertFalse(AuditEvent.objects.filter(summary__icontains="A-Brand-New").exists())

    def test_wrong_old_password_rejected(self):
        r = self.change(old="not-my-password-1")
        self.assertEqual(r.status_code, 200)
        self.user.refresh_from_db()
        self.assertTrue(self.user.must_change_password)

    def test_same_password_rejected(self):
        r = self.change(new=PW)
        self.assertContains(r, "must be different")
        self.user.refresh_from_db()
        self.assertTrue(self.user.must_change_password)

    def test_weak_password_rejected(self):
        for weak in ("short1", "1234567890123", "password1234"):
            self.change(new=weak)
        self.user.refresh_from_db()
        self.assertTrue(self.user.must_change_password)
        self.assertTrue(self.user.check_password(PW))
