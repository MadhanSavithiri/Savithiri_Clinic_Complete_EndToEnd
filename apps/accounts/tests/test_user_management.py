from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from apps.core.models import AuditEvent

from .helpers import PW, make_user

User = get_user_model()


class AccessControlTests(TestCase):
    def setUp(self):
        self.target = make_user("victim", "pharmacy")
        self.urls = [
            reverse("accounts:user_list"), reverse("accounts:user_create"),
            reverse("accounts:user_detail", args=[self.target.pk]),
            reverse("accounts:user_edit", args=[self.target.pk]),
            reverse("accounts:user_reset_password", args=[self.target.pk]),
            reverse("accounts:user_toggle_active", args=[self.target.pk]),
        ]

    def test_anonymous_is_sent_to_login(self):
        for url in self.urls:
            r = self.client.get(url)
            self.assertEqual(r.status_code, 302, url)
            self.assertIn(reverse("accounts:login"), r["Location"])

    def test_non_admin_roles_get_403_and_it_is_audited(self):
        for role in ("doctor", "reception", "pharmacy"):
            user = make_user(f"u_{role}", role)
            self.client.force_login(user)
            for url in self.urls:
                self.assertEqual(self.client.get(url).status_code, 403, f"{role} {url}")
            self.assertTrue(AuditEvent.objects.filter(action="auth.permission_denied", username=user.username).exists())

    def test_non_admin_cannot_post_actions(self):
        self.client.force_login(make_user("rec", "reception"))
        r = self.client.post(reverse("accounts:user_reset_password", args=[self.target.pk]))
        self.assertEqual(r.status_code, 403)
        r = self.client.post(reverse("accounts:user_toggle_active", args=[self.target.pk]))
        self.assertEqual(r.status_code, 403)
        r = self.client.post(reverse("accounts:user_unlock", args=[self.target.pk]))
        self.assertEqual(r.status_code, 403)
        self.target.refresh_from_db()
        self.assertTrue(self.target.is_active)
        self.assertTrue(self.target.check_password(PW))


class StaffManagementTests(TestCase):
    def setUp(self):
        self.admin = make_user("boss", "admin")
        self.client.force_login(self.admin)

    def create(self, **over):
        data = {"username": "nurse1", "first_name": "Nila", "last_name": "K", "role": "reception", "phone": ""}
        data.update(over)
        return self.client.post(reverse("accounts:user_create"), data)

    def test_admin_sees_list(self):
        self.assertContains(self.client.get(reverse("accounts:user_list")), "boss")

    def test_create_user_generates_hashed_temp_password_shown_once(self):
        r = self.create()
        self.assertEqual(r.status_code, 200)
        temp = r.context["temp_password"]
        self.assertGreaterEqual(len(temp), 12)
        self.assertContains(r, temp)
        user = User.objects.get(username="nurse1")
        self.assertEqual(user.role, "reception")
        self.assertTrue(user.must_change_password)
        self.assertFalse(user.is_superuser or user.is_staff)
        self.assertNotEqual(user.password, temp)
        self.assertTrue(user.check_password(temp))
        # password must never be written to the audit log
        self.assertFalse(AuditEvent.objects.filter(summary__contains=temp).exists())
        self.assertFalse(any(temp in str(e.changes) for e in AuditEvent.objects.all()))
        self.assertTrue(AuditEvent.objects.filter(action="user.create", username="boss").exists())

    def test_temp_password_is_not_cached(self):
        self.assertIn("no-store", self.create()["Cache-Control"])

    def test_new_user_can_sign_in_only_to_change_password(self):
        temp = self.create().context["temp_password"]
        self.client.logout()
        self.client.post(reverse("accounts:login"), {"username": "nurse1", "password": temp})
        r = self.client.get(reverse("home"))
        self.assertRedirects(r, reverse("accounts:password_change"), fetch_redirect_response=False)

    def test_duplicate_username_any_case_rejected(self):
        self.create()
        r = self.create(username="NURSE1")
        self.assertContains(r, "already taken")
        self.assertEqual(User.objects.filter(username__iexact="nurse1").count(), 1)

    def test_creating_admin_role_sets_admin_flags(self):
        self.create(username="boss2", role="admin")
        u = User.objects.get(username="boss2")
        self.assertTrue(u.is_staff and u.is_superuser)

    def test_edit_changes_role_flags_and_audits_diff(self):
        self.create()
        u = User.objects.get(username="nurse1")
        r = self.client.post(reverse("accounts:user_edit", args=[u.pk]),
                             {"first_name": "Nila", "last_name": "K", "role": "pharmacy", "phone": "99999"})
        self.assertEqual(r.status_code, 302)
        u.refresh_from_db()
        self.assertEqual((u.role, u.phone), ("pharmacy", "99999"))
        ev = AuditEvent.objects.get(action="user.update")
        self.assertEqual(ev.changes["role"], ["reception", "pharmacy"])

    def test_cannot_change_own_role(self):
        r = self.client.post(reverse("accounts:user_edit", args=[self.admin.pk]),
                             {"first_name": "Boss", "last_name": "", "role": "doctor", "phone": ""})
        self.admin.refresh_from_db()
        self.assertEqual(self.admin.role, "admin")

    def test_cannot_demote_last_active_admin(self):
        # a second admin tries to demote the only OTHER admin after the first is deactivated
        other = make_user("boss2", "admin")
        User.objects.filter(pk=self.admin.pk).update(is_active=False)
        self.client.force_login(other)
        r = self.client.post(reverse("accounts:user_edit", args=[other.pk]),
                             {"first_name": "Boss2", "last_name": "", "role": "doctor", "phone": ""})
        other.refresh_from_db()
        self.assertEqual(other.role, "admin")

    def test_deactivate_and_reactivate_other_user(self):
        u = make_user("pharm", "pharmacy")
        url = reverse("accounts:user_toggle_active", args=[u.pk])
        self.assertEqual(self.client.get(url).status_code, 200)  # confirmation page
        self.client.post(url)
        u.refresh_from_db()
        self.assertFalse(u.is_active)
        self.client.post(url)
        u.refresh_from_db()
        self.assertTrue(u.is_active)
        self.assertTrue(AuditEvent.objects.filter(action="user.deactivate").exists())
        self.assertTrue(AuditEvent.objects.filter(action="user.activate").exists())

    def test_cannot_deactivate_self(self):
        self.client.post(reverse("accounts:user_toggle_active", args=[self.admin.pk]))
        self.admin.refresh_from_db()
        self.assertTrue(self.admin.is_active)

    def test_cannot_deactivate_last_active_admin(self):
        other = make_user("boss2", "admin")
        User.objects.filter(pk=other.pk).update(is_active=False)
        self.client.post(reverse("accounts:user_toggle_active", args=[self.admin.pk]))
        self.admin.refresh_from_db()
        self.assertTrue(self.admin.is_active)

    def test_reset_password_replaces_password_unlocks_and_forces_change(self):
        u = make_user("doc", "doctor")
        User.objects.filter(pk=u.pk).update(failed_login_count=3, locked_until=timezone.now() + timedelta(minutes=9))
        url = reverse("accounts:user_reset_password", args=[u.pk])
        self.assertEqual(self.client.get(url).status_code, 200)
        r = self.client.post(url)
        temp = r.context["temp_password"]
        u.refresh_from_db()
        self.assertFalse(u.check_password(PW))
        self.assertTrue(u.check_password(temp))
        self.assertTrue(u.must_change_password)
        self.assertFalse(u.is_locked())
        self.assertEqual(u.failed_login_count, 0)
        self.assertTrue(AuditEvent.objects.filter(action="user.reset_password").exists())
        self.assertFalse(any(temp in (e.summary + str(e.changes)) for e in AuditEvent.objects.all()))

    def test_reset_ends_the_other_persons_open_session(self):
        u = make_user("doc", "doctor")
        from django.test import Client
        other = Client()
        other.force_login(u)
        self.assertEqual(other.get(reverse("home")).status_code, 200)
        self.client.post(reverse("accounts:user_reset_password", args=[u.pk]))
        self.assertEqual(other.get(reverse("home")).status_code, 302)

    def test_cannot_reset_own_password_here(self):
        self.client.post(reverse("accounts:user_reset_password", args=[self.admin.pk]))
        self.admin.refresh_from_db()
        self.assertTrue(self.admin.check_password(PW))

    def test_unlock(self):
        u = make_user("doc", "doctor")
        User.objects.filter(pk=u.pk).update(locked_until=timezone.now() + timedelta(minutes=9))
        self.client.post(reverse("accounts:user_unlock", args=[u.pk]))
        u.refresh_from_db()
        self.assertFalse(u.is_locked())
        self.assertEqual(self.client.get(reverse("accounts:user_unlock", args=[u.pk])).status_code, 405)


class HomePageTests(TestCase):
    def test_each_role_sees_only_its_areas(self):
        expectations = {
            "admin": (["Staff accounts", "Audit log"], ["Consultation", "Dispensing", "Billing", "Register a patient"]),
            "doctor": (["Consultation", "Today's queue"], ["Staff accounts", "Dispensing", "Billing", "Register a patient"]),
            "reception": (["Register a patient", "Billing"], ["Consultation", "Staff accounts", "Dispensing"]),
            "pharmacy": (["Dispensing", "Medicine catalogue", "Stock and expiry"], ["Consultation", "Staff accounts", "Register a patient"]),
        }
        for role, (shown, hidden) in expectations.items():
            self.client.force_login(make_user(f"x_{role}", role))
            r = self.client.get(reverse("home"))
            for text in shown:
                self.assertContains(r, text, msg_prefix=role)
            for text in hidden:
                self.assertNotContains(r, text, msg_prefix=role)
