"""BMI categories. Cut-offs are published adult values; the clinic can pick the set in Settings
(key `bmi_standard`: WHO or ASIAN). Adult categories are NOT shown for children and adolescents."""
from datetime import date
from decimal import Decimal

from apps.core.models import Setting

STANDARDS = {
    "WHO": {"label": "WHO adult cut-offs", "under": Decimal("18.5"), "over": Decimal("25"), "obese": Decimal("30")},
    "ASIAN": {"label": "Asian adult cut-offs", "under": Decimal("18.5"), "over": Decimal("23"), "obese": Decimal("25")},
}


def standard_key():
    key = str(Setting.get("bmi_standard", "WHO")).upper()
    return key if key in STANDARDS else "WHO"


def cutoffs(key=None):
    return STANDARDS[key or standard_key()]


def bmi_category(bmi, key=None):
    """Underweight / Normal / Overweight / Obese for an adult BMI (a Decimal), or '' if there is no BMI."""
    if bmi is None:
        return ""
    c = cutoffs(key)
    if bmi < c["under"]:
        return "Underweight"
    if bmi < c["over"]:
        return "Normal"
    if bmi < c["obese"]:
        return "Overweight"
    return "Obese"


def is_adult(patient, today=None):
    dob = patient.date_of_birth
    if dob is None:
        return True  # registration always records a date of birth or an estimate; be safe if it is missing
    today = today or date.today()
    years = today.year - dob.year - ((today.month, today.day) < (dob.month, dob.day))
    return years >= 18


def bmi_text(bmi, adult, key=None):
    if bmi is None:
        return ""
    if not adult:
        return "Child or adolescent: use age- and sex-specific growth charts"
    return bmi_category(bmi, key)
