from django.contrib import admin

from .models import AuditEvent, ClinicProfile, DoctorProfile, DocumentSequence, Setting


@admin.register(ClinicProfile)
class ClinicProfileAdmin(admin.ModelAdmin):
    def has_add_permission(self, request):
        return not ClinicProfile.objects.exists()

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(DoctorProfile)
class DoctorProfileAdmin(admin.ModelAdmin):
    list_display = ("full_name", "qualifications", "registration_number", "is_active")


@admin.register(Setting)
class SettingAdmin(admin.ModelAdmin):
    list_display = ("key", "value", "description")


@admin.register(DocumentSequence)
class DocumentSequenceAdmin(admin.ModelAdmin):
    list_display = ("doc_type", "period", "prefix", "padding", "next_value")
    readonly_fields = ("doc_type", "period", "next_value")

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(AuditEvent)
class AuditEventAdmin(admin.ModelAdmin):
    list_display = ("timestamp", "username", "action", "object_type", "object_repr", "ip_address")
    list_filter = ("action",)
    search_fields = ("username", "object_repr", "summary")
    date_hierarchy = "timestamp"

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
