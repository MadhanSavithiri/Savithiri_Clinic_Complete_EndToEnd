"""Sale rules that are easy to review in one place. These are software controls only: the exact legal
requirements for prescription-only medicines must be confirmed with the Tamil Nadu Drugs Control Department."""
from apps.pharmacy.models import ScheduleCategory

# Need a pharmacist's approval and the prescriber / patient details before they can be sold at the counter.
APPROVAL_CATEGORIES = {ScheduleCategory.H, ScheduleCategory.H1, ScheduleCategory.OTHER}
# Never sold at the independent counter (unless the setting `counter_allow_schedule_x` is turned on).
BLOCKED_CATEGORIES = {ScheduleCategory.X}


def needs_approval(category):
    """Schedule X can only reach a bill if the clinic switched it on, and then it needs approval like the others."""
    return category in APPROVAL_CATEGORIES or category in BLOCKED_CATEGORIES
