from django.contrib.auth.models import AnonymousUser
from django.core.exceptions import ImproperlyConfigured
from django.test import SimpleTestCase, TestCase

from apps.accounts.permissions import ALL_PERMISSIONS, ROLE_PERMISSIONS, user_has_permission

from .helpers import make_user


class RoleMatrixTests(TestCase):
    def setUp(self):
        self.admin = make_user("a", "admin")
        self.doctor = make_user("d", "doctor")
        self.reception = make_user("r", "reception")
        self.pharmacy = make_user("p", "pharmacy")
        self.manager = make_user("m", "pharmacy_manager")
        self.counter = make_user("c", "counter")

    def assertCan(self, user, *codes):
        for code in codes:
            self.assertTrue(user_has_permission(user, code), f"{user.role} should have {code}")

    def assertCannot(self, user, *codes):
        for code in codes:
            self.assertFalse(user_has_permission(user, code), f"{user.role} must NOT have {code}")

    def test_reception_cannot_touch_clinical_or_stock_or_refund_approval(self):
        self.assertCannot(self.reception, "queue.consult", "clinical.view_notes", "clinical.write_notes", "clinical.prescribe",
                          "pharmacy.dispense", "pharmacy.adjust_stock", "billing.approve_refund",
                          "accounts.manage_users", "reports.view_financial")
        self.assertCan(self.reception, "patients.register", "queue.add", "queue.cancel", "billing.record_payment",
                       "billing.request_refund")

    def test_pharmacy_cannot_alter_clinical_notes_or_prescriptions(self):
        self.assertCannot(self.pharmacy, "clinical.view_notes", "clinical.write_notes", "clinical.prescribe",
                          "clinical.authorize_correction", "patients.view", "accounts.manage_users")
        self.assertCan(self.pharmacy, "pharmacy.view_prescriptions", "pharmacy.dispense", "patients.view_basic")
        self.assertCannot(self.pharmacy, "pharmacy.adjust_stock", "pharmacy.manage_purchases", "pharmacy.manage_catalogue",
                          "pharmacy.view_costs")

    def test_doctor_has_clinical_but_not_money_or_dispensing(self):
        self.assertCan(self.doctor, "clinical.view_notes", "clinical.write_notes", "clinical.prescribe",
                       "clinical.authorize_correction", "pharmacy.view_prescriptions")
        self.assertCannot(self.doctor, "queue.add", "patients.register", "pharmacy.dispense", "pharmacy.adjust_stock", "billing.approve_refund",
                          "billing.record_payment", "reports.view_financial", "accounts.manage_users")

    def test_admin_runs_the_clinic_but_cannot_read_or_write_clinical_notes(self):
        self.assertCan(self.admin, "accounts.manage_users", "core.view_audit", "reports.view_financial",
                       "billing.approve_refund", "pharmacy.adjust_stock")
        self.assertCannot(self.admin, "clinical.view_notes", "clinical.write_notes", "clinical.prescribe",
                          "pharmacy.dispense")

    def test_purchase_prices_and_valuation_are_for_admin_and_pharmacy_only(self):
        self.assertCan(self.admin, "pharmacy.view_costs")
        self.assertCan(self.manager, "pharmacy.view_costs")
        self.assertCannot(self.pharmacy, "pharmacy.view_costs")
        self.assertCannot(self.counter, "pharmacy.view_costs")
        self.assertCannot(self.doctor, "pharmacy.view_costs", "pharmacy.manage_purchases", "pharmacy.adjust_stock")
        self.assertCannot(self.reception, "pharmacy.view_costs", "pharmacy.view_stock")

    def test_counter_salesperson_can_sell_and_collect_and_nothing_else(self):
        self.assertCan(self.counter, "sales.counter_sell", "sales.view_own", "sales.apply_discount")
        self.assertCannot(
            self.counter, "pharmacy.view_costs", "pharmacy.view_stock", "pharmacy.manage_catalogue", "pharmacy.adjust_stock",
            "pharmacy.manage_purchases", "pharmacy.dispense", "sales.view_all", "sales.approve_restricted",
            "clinical.view_notes", "patients.view", "patients.view_basic", "queue.view", "accounts.manage_users",
            "core.manage_settings", "reports.view_financial", "core.view_audit")

    def test_manager_runs_the_pharmacy_but_cannot_touch_clinical_or_staff(self):
        self.assertCan(self.manager, "pharmacy.manage_catalogue", "pharmacy.adjust_stock", "pharmacy.manage_purchases",
                       "pharmacy.view_costs", "sales.view_all", "sales.approve_restricted", "sales.counter_sell")
        self.assertCannot(self.manager, "clinical.view_notes", "accounts.manage_users", "patients.view", "core.view_audit")

    def test_clinic_pharmacist_can_approve_restricted_sales_but_not_sell_at_the_counter(self):
        self.assertCan(self.pharmacy, "sales.approve_restricted")
        self.assertCannot(self.pharmacy, "sales.counter_sell", "sales.apply_discount")

    def test_admin_sees_all_counter_sales_but_cannot_sell(self):
        self.assertCan(self.admin, "sales.view_all")
        self.assertCannot(self.admin, "sales.counter_sell", "sales.approve_restricted")

    def test_inactive_user_has_nothing(self):
        self.doctor.is_active = False
        self.doctor.save()
        self.assertCannot(self.doctor, "clinical.view_notes", "queue.view")

    def test_anonymous_has_nothing(self):
        self.assertFalse(user_has_permission(AnonymousUser(), "queue.view"))
        self.assertFalse(user_has_permission(None, "queue.view"))


class CatalogueConsistencyTests(SimpleTestCase):
    def test_every_granted_code_exists(self):
        for role, codes in ROLE_PERMISSIONS.items():
            self.assertLessEqual(codes, set(ALL_PERMISSIONS), f"{role} has unknown permission codes")

    def test_every_role_is_defined(self):
        from apps.accounts.models import User
        self.assertEqual(set(ROLE_PERMISSIONS), set(User.Role.values))

    def test_typos_fail_loudly(self):
        with self.assertRaises(ImproperlyConfigured):
            user_has_permission(None, "clinical.view_notez")
