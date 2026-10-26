from .models import ClinicProfile


def clinic(request):
    """Clinic name/address for page headers. Read-only (never writes on a page view)."""
    return {"clinic": ClinicProfile.objects.first() or ClinicProfile()}
