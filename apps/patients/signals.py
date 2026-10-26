from django.dispatch import Signal

# Sent inside the merge transaction so other apps can keep their own rules consistent.
patient_merged = Signal()  # kwargs: target, source, moved, actor
