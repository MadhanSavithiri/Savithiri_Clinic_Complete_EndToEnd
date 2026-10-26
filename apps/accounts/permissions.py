"""Role-based permissions (least privilege).

The catalogue below is the single source of truth. A role gets ONLY what is listed.
Deliberately independent of Django's is_superuser: even the Clinic Administrator has no
access to clinical notes here. (Django's technical /admin/ site is separate.)
Changing this file changes who can do what: review with the doctor/owner first.
"""
from django.core.exceptions import ImproperlyConfigured

from .models import User

R = User.Role

ALL_PERMISSIONS = {
    # accounts / settings / audit
    "accounts.manage_users": "Create, edit, deactivate staff and reset passwords",
    "core.manage_settings": "Change clinic profile, numbering and settings",
    "core.view_audit": "View the audit log",
    # patients
    "patients.view": "View full patient demographics",
    "patients.view_basic": "View patient name, ID, age and sex only",
    "patients.register": "Register new patients",
    "patients.edit": "Edit patient demographics",
    "patients.merge": "Merge duplicate patient records",
    # queue
    "queue.view": "View the queue",
    "queue.add": "Book appointments, register walk-ins and issue tokens",
    "queue.cancel": "Cancel queue tokens and appointments (reason required)",
    "queue.consult": "Call, return and complete patients in the queue",
    # clinical
    "clinical.view_notes": "View clinical notes, vitals and history",
    "clinical.write_notes": "Record consultations, vitals and notes",
    "clinical.prescribe": "Create, edit and finalize prescriptions",
    "clinical.authorize_correction": "Authorize correction of a finalized prescription",
    # pharmacy
    "pharmacy.view_prescriptions": "View finalized prescriptions for dispensing",
    "pharmacy.dispense": "Dispense medicines",
    "pharmacy.manage_catalogue": "Add and edit the medicine catalogue, prices and rack/shelf/box locations",
    "pharmacy.view_stock": "View stock levels and batches",
    "pharmacy.adjust_stock": "Adjust stock, record wastage and returns",
    "pharmacy.manage_purchases": "Enter purchases and supplier invoices",
    "pharmacy.view_costs": "See purchase prices, stock valuation and purchase reports",
    # independent counter sales
    "sales.counter_sell": "Create and complete independent counter-sales bills and take payment",
    "sales.view_own": "View own counter sales, receipts and shift",
    "sales.view_all": "View all counter sales and shifts",
    "sales.apply_discount": "Apply a discount within the set limit (reason required)",
    "sales.approve_restricted": "Approve the sale of prescription-only medicines",
    # billing
    "billing.create_consultation_invoice": "Create consultation invoices",
    "billing.create_pharmacy_invoice": "Create pharmacy bills",
    "billing.record_payment": "Record payments",
    "billing.view_invoices": "View invoices and receipts",
    "billing.request_refund": "Request a refund or cancellation",
    "billing.approve_refund": "Approve and process refunds",
    "billing.close_day": "Close the day's accounts",
    # reports
    "reports.view_cashier": "View the cashier closing report",
    "reports.view_financial": "View financial reports",
    "reports.view_statistics": "View patient and visit statistics",
}

ROLE_PERMISSIONS = {
    R.ADMIN: frozenset({
        "accounts.manage_users", "core.manage_settings", "core.view_audit",
        "patients.view", "patients.merge", "queue.view",
        "pharmacy.view_stock", "pharmacy.adjust_stock", "pharmacy.manage_purchases", "pharmacy.view_costs",
        "sales.view_all",
        "billing.view_invoices", "billing.approve_refund",
        "reports.view_cashier", "reports.view_financial", "reports.view_statistics",
    }),
    R.DOCTOR: frozenset({
        "patients.view", "queue.view", "queue.cancel", "queue.consult",
        "clinical.view_notes", "clinical.write_notes", "clinical.prescribe",
        "clinical.authorize_correction",
        "pharmacy.view_prescriptions", "pharmacy.view_stock",
        "reports.view_statistics",
    }),
    R.RECEPTION: frozenset({
        "patients.view", "patients.register", "patients.edit",
        "queue.view", "queue.add", "queue.cancel",
        "billing.create_consultation_invoice", "billing.record_payment",
        "billing.view_invoices", "billing.request_refund", "billing.close_day",
        "reports.view_cashier",
    }),
    # Clinic pharmacist: dispenses the doctor's prescriptions; can approve prescription-only sales.
    R.PHARMACY: frozenset({
        "patients.view_basic",
        "pharmacy.view_prescriptions", "pharmacy.dispense", "pharmacy.view_stock",
        "sales.approve_restricted",
        "billing.create_pharmacy_invoice", "billing.record_payment", "billing.view_invoices",
    }),
    # Pharmacy manager: catalogue, prices, locations, purchases, stock corrections, costs, approvals.
    R.PHARMACY_MANAGER: frozenset({
        "patients.view_basic",
        "pharmacy.view_prescriptions", "pharmacy.dispense",
        "pharmacy.manage_catalogue", "pharmacy.view_stock", "pharmacy.adjust_stock",
        "pharmacy.manage_purchases", "pharmacy.view_costs",
        "sales.counter_sell", "sales.view_own", "sales.view_all", "sales.apply_discount", "sales.approve_restricted",
        "billing.create_pharmacy_invoice", "billing.record_payment", "billing.view_invoices",
    }),
    # Independent counter: sells and collects money. Nothing else. No costs, no stock pages.
    R.COUNTER: frozenset({
        "sales.counter_sell", "sales.view_own", "sales.apply_discount",
    }),
}


def user_has_permission(user, code):
    """True only for an active, signed-in user whose role grants `code`."""
    if code not in ALL_PERMISSIONS:
        raise ImproperlyConfigured(f"Unknown permission code: {code!r}")
    if user is None or not getattr(user, "is_authenticated", False) or not user.is_active:
        return False
    return code in ROLE_PERMISSIONS.get(user.role, frozenset())
