from datetime import date

from apps.accounts.tests.helpers import make_user
from apps.clinical import services
from apps.core.models import DoctorProfile
from apps.patients.tests.helpers import make_patient
from apps.pharmacy.models import Medicine
from apps.queueing import services as queue_services


class World:
    """A small clinic: users, one patient, and helpers to move through the workflow."""

    def __init__(self):
        self.reception = make_user("rita", "reception")
        self.doctor = make_user("doc", "doctor")
        self.pharmacy = make_user("pharm", "pharmacy")
        self.admin = make_user("boss", "admin")
        self.patient = make_patient(self.reception, name="Anbu Selvan", phone="9000000001", dob=date(1980, 1, 1))
        # Obviously fake test data. The real profile is entered by the clinic.
        self.profile = DoctorProfile.objects.create(full_name="Dr TEST Doctor", qualifications="MBBS (test)",
                                                    registration_number="TEST-REG-0001", user=self.doctor)

    def token(self, patient=None):
        return queue_services.add_to_queue(actor=self.reception, request=None, patient=patient or self.patient)

    def encounter(self, patient=None):
        return services.start_consultation(actor=self.doctor, request=None, token_id=self.token(patient).pk)

    def fill_notes(self, enc, **over):
        data = {"presenting_complaints": "Fever for 2 days", "symptom_duration": "2 days", "examination_findings": "",
                "assessment": "Viral fever", "treatment_plan": "", "advice": "Rest and fluids",
                "follow_up_date": None, "follow_up_notes": ""}
        data.update(over)
        return services.update_notes(actor=self.doctor, request=None, encounter_id=enc.pk, data=data)

    def item_data(self, **over):
        data = {"medicine": None, "medicine_name": "Test Tablet", "strength": "500 mg", "dosage_form": "Tablet",
                "dose": "1 tablet", "route": "Oral", "frequency": "1-0-1", "duration": "3 days", "quantity": 6,
                "instructions": "After food"}
        data.update(over)
        return data

    def add_item(self, enc, **over):
        return services.add_item(actor=self.doctor, request=None, encounter_id=enc.pk, data=self.item_data(**over))

    def issue(self, enc, **over):
        item = self.add_item(enc, **over)
        return services.finalize_prescription(actor=self.doctor, request=None, prescription_id=item.prescription_id)


def make_medicine(**over):
    data = {"brand_name": "TestBrand", "generic_name": "testgeneric", "strength": "250 mg", "dosage_form": "Tablet"}
    data.update(over)
    return Medicine.objects.create(**data)
