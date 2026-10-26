from datetime import date

from apps.patients import services


def make_patient(actor, name="Ramesh Kumar", phone="9876543210", dob=date(1985, 3, 10), sex="M", **extra):
    data = {"full_name": name, "sex": sex, "date_of_birth": dob, "dob_estimated": False, "phone": phone, "address": ""}
    data.update(extra)
    return services.register_patient(actor=actor, request=None, data=data)


def registration_post(**over):
    data = {
        "full_name": "Ramesh Kumar", "sex": "M", "date_of_birth": "1985-03-10", "age_years": "",
        "phone": "98765 43210", "address": "Main Street, Lalgudi",
        "emergency_contact_name": "", "emergency_contact_phone": "", "emergency_contact_relation": "",
        "consent_given": "on", "consent_given_by": "Self",
    }
    data.update(over)
    return {k: v for k, v in data.items() if v is not None}
