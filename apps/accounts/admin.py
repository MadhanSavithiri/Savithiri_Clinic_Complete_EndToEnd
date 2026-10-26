from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as DjangoUserAdmin

from .models import User


@admin.register(User)
class UserAdmin(DjangoUserAdmin):
    fieldsets = DjangoUserAdmin.fieldsets + (
        ("Clinic role", {"fields": ("role", "phone", "must_change_password")}),
        ("Sign-in protection", {"fields": ("failed_login_count", "locked_until")}),
    )
    add_fieldsets = DjangoUserAdmin.add_fieldsets + (
        ("Clinic role", {"fields": ("role", "phone", "first_name", "last_name")}),
    )
    readonly_fields = ("failed_login_count", "locked_until")
    list_display = ("username", "get_full_name", "role", "is_active", "last_login")
    list_filter = ("role", "is_active")

    def has_delete_permission(self, request, obj=None):
        # Staff are deactivated, not deleted, so history stays attributable.
        return False
